"""Safety layer: path jail, secret-file blocking, command allowlist.

IMPORTANT: this is a *policy* layer, not an OS-level security boundary. Anything that lets the
agent run code (python scripts, pytest) can in principle do anything your user account can do.
For untrusted tasks or repos, run the whole agent inside a container or VM.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


class GuardError(Exception):
    """An action violated the safety policy. The message is shown to the model."""


# ---------------------------------------------------------------- paths & secrets

SECRET_NAMES = {".env", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def is_secret_path(p) -> bool:
    name = Path(str(p)).name.lower()
    return name in SECRET_NAMES or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES)


def real(p) -> Path:
    return Path(os.path.realpath(p))


def is_inside(root: Path, resolved: Path) -> bool:
    root = real(root)
    return resolved == root or root in resolved.parents


def resolve_in_sandbox(root: Path, path: str) -> Path:
    """Resolve `path` (following symlinks) and require it to be inside `root`."""
    if not isinstance(path, str) or not path.strip():
        raise GuardError("path must be a non-empty string")
    if "\x00" in path:
        raise GuardError("invalid path")
    root_real = real(root)
    cand = Path(path)
    if not cand.is_absolute():
        cand = root_real / cand
    resolved = real(cand)
    if not is_inside(root_real, resolved):
        raise GuardError(f"path '{path}' is outside the sandbox root")
    return resolved


def check_writable(root: Path, resolved: Path) -> None:
    rel = resolved.relative_to(real(root))
    if any(part.startswith(".") for part in rel.parts):
        raise GuardError("writing to dotfiles or hidden directories (including .git/) is not allowed")
    if is_secret_path(resolved):
        raise GuardError("writing to secrets-like files is not allowed")


# ---------------------------------------------------------------- commands

SHELL_OPERATORS = [";", "&", "|", ">", "<", "`", "$(", "\n", "\r"]
ALLOWED_SUMMARY = (
    "python/python3 (script.py or -m pytest/unittest), pytest, pip list/show/freeze, "
    "git status/diff/log/show, ls, cat, grep, find (without -delete/-exec)"
)
GIT_SUBCOMMANDS = {"status", "diff", "log", "show"}
GIT_BLOCKED_ARGS = ("-c", "--exec-path", "--ext-diff", "--textconv")
FIND_BLOCKED = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
PYTEST_BLOCKED = ("-p", "--basetemp", "--rootdir", "--confcutdir", "--override-ini", "-o")
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


def _check_path_arg(root: Path, arg: str) -> None:
    if not arg:
        return
    if arg.startswith("~"):
        raise GuardError("'~' paths are not allowed")
    if is_secret_path(arg):
        raise GuardError(f"'{arg}' looks like a secrets file")
    root_real = real(root)
    p = Path(arg)
    cand = p if p.is_absolute() else root_real / p
    suspicious = p.is_absolute() or ".." in p.parts
    if suspicious or os.path.lexists(cand):
        if not is_inside(root_real, real(cand)):
            raise GuardError(f"argument '{arg}' points outside the sandbox root")


def parse_command(root: Path, command: str) -> list[str]:
    """Validate a command string against the allowlist and return argv (never run via a shell)."""
    if not isinstance(command, str) or not command.strip():
        raise GuardError("empty command")
    for op in SHELL_OPERATORS:
        if op in command:
            raise GuardError(
                f"shell operators are not allowed (found {op.strip() or 'a newline'!r}); "
                "run one plain command at a time"
            )
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise GuardError(f"could not parse command: {e}")
    if not argv:
        raise GuardError("empty command")

    prog, args = argv[0], argv[1:]

    if prog in ("python", "python3"):
        if args in (["--version"], ["-V"]):
            return argv
        if not args:
            raise GuardError("interactive python is not allowed; pass a script or -m pytest")
        if args[0] == "-m":
            if len(args) < 2 or args[1] not in ("pytest", "unittest"):
                raise GuardError("only 'python -m pytest' and 'python -m unittest' are allowed")
            if args[1] == "pytest":
                _check_pytest_args(args[2:])
        elif args[0].startswith("-"):
            raise GuardError(f"python option '{args[0]}' is not allowed (no -c or stdin code)")
        elif not args[0].endswith(".py"):
            raise GuardError("python can only run .py scripts inside the sandbox")
    elif prog == "pytest":
        _check_pytest_args(args)
    elif prog == "pip":
        if not args or args[0] not in ("list", "show", "freeze"):
            raise GuardError("only 'pip list', 'pip show' and 'pip freeze' are allowed")
    elif prog == "git":
        if not args or args[0] not in GIT_SUBCOMMANDS:
            raise GuardError("only git status/diff/log/show are allowed")
        for a in args:
            if a in GIT_BLOCKED_ARGS or a.startswith("--output") or a.startswith("--exec-path"):
                raise GuardError(f"git option '{a}' is not allowed")
    elif prog == "find":
        for a in args:
            if a in FIND_BLOCKED:
                raise GuardError(f"find option '{a}' is not allowed")
    elif prog in ("ls", "cat", "grep"):
        pass
    else:
        raise GuardError(f"command '{prog}' is not allowed. Allowed: {ALLOWED_SUMMARY}")

    for a in args:
        if a.startswith("-") and "=" in a:
            _check_path_arg(root, a.split("=", 1)[1])
        elif not a.startswith("-"):
            _check_path_arg(root, a)
    return argv


def _check_pytest_args(args: list[str]) -> None:
    for a in args:
        if a in PYTEST_BLOCKED or any(a.startswith(b + "=") for b in PYTEST_BLOCKED if b.startswith("--")):
            raise GuardError(f"pytest option '{a}' is not allowed")


def scrubbed_env() -> dict:
    """Environment for child processes: drop anything that looks like a credential."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env
