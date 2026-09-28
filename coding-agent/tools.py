"""Tool schemas and executors. Every executor runs behind the guard layer."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from config import Config
from guards import (GuardError, check_writable, is_inside, is_secret_path, parse_command, real,
                    resolve_in_sandbox, scrubbed_env)

MAX_READ_BYTES = 1_000_000
MAX_READ_LINES = 400
MAX_WRITE_BYTES = 200_000
MAX_MATCHES = 50
MAX_OUTPUT_CHARS = 8192
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "runs", ".pytest_cache"}


class ToolError(Exception):
    """Ordinary, expected failure (bad input, file missing). Returned to the model as an error."""


TOOL_SCHEMAS = [
    {
        "name": "list_dir",
        "description": "List files and directories at a path (directories end with '/').",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path relative to the project root. Default '.'"}}},
    },
    {
        "name": "read_file",
        "description": "Read a text file with line numbers. Output is capped at 400 lines; use start_line/end_line to page.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line (optional)"},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive (optional)"}},
            "required": ["path"]},
    },
    {
        "name": "search_code",
        "description": "Regex search across files. Returns up to 50 matches as 'file:line: text'.",
        "input_schema": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "File or directory to search. Default '.'"},
            "glob": {"type": "string", "description": "Only search files whose name matches, e.g. '*.py'"}},
            "required": ["pattern"]},
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_str with new_str in an existing file. "
                       "Fails unless old_str appears exactly once (include surrounding lines to make it unique).",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]},
    },
    {
        "name": "write_file",
        "description": "Create a new file. Refuses to replace an existing file unless overwrite=true, "
                       "which asks the user for confirmation. Prefer edit_file for changes to existing files.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "overwrite": {"type": "boolean", "description": "Default false"}},
            "required": ["path", "content"]},
    },
    {
        "name": "run_command",
        "description": "Run one allowlisted command in the project root (no shell, no pipes/redirects). "
                       "Allowed: python script.py, python -m pytest, pytest, pip list/show/freeze, "
                       "git status/diff/log/show, ls, cat, grep, find. Max timeout 60s.",
        "input_schema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer", "description": "Seconds, default 30, max 60"}},
            "required": ["command"]},
    },
]


def _truncate(s: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n[... {len(s) - limit} characters truncated ...]\n{s[-half:]}"


class Tools:
    def __init__(self, root, cfg: Config, confirm: Callable[[str], bool]):
        self.root = real(root)
        self.cfg = cfg
        self.confirm = confirm
        self.changed: set[Path] = set()
        self.guard_blocks = 0

    # ------------------------------------------------------------ dispatch
    def execute(self, name: str, args) -> tuple[str, bool]:
        """Run a tool. Returns (text, is_error). Never raises."""
        fn = getattr(self, f"_t_{name}", None) if name in {s["name"] for s in TOOL_SCHEMAS} else None
        if fn is None:
            return f"Unknown tool '{name}'", True
        if not isinstance(args, dict):
            return "Tool input must be a JSON object", True
        try:
            return fn(**args), False
        except GuardError as e:
            self.guard_blocks += 1
            return f"BLOCKED by safety policy: {e}", True
        except ToolError as e:
            return str(e), True
        except TypeError as e:
            return f"Invalid arguments for {name}: {e}", True
        except Exception as e:  # keep the loop alive no matter what
            return f"{name} failed: {type(e).__name__}: {e}", True

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    # ------------------------------------------------------------ read-only tools
    def _t_list_dir(self, path: str = ".") -> str:
        p = resolve_in_sandbox(self.root, path)
        if not p.is_dir():
            raise ToolError(f"'{path}' is not a directory")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [e.name + ("/" if e.is_dir() else "") for e in entries[:200]]
        if len(entries) > 200:
            lines.append(f"... {len(entries) - 200} more entries not shown")
        return "\n".join(lines) or "(empty directory)"

    def _t_read_file(self, path: str, start_line=None, end_line=None) -> str:
        p = resolve_in_sandbox(self.root, path)
        if is_secret_path(p):
            raise GuardError("reading secrets files is not allowed")
        if not p.is_file():
            raise ToolError(f"'{path}' is not a file or does not exist")
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(f"file is {size} bytes, too large to read whole; use search_code or read_file with a line range on a smaller file")
        data = p.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError("file looks binary; cannot read it as text")
        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return f"{self._rel(p)} (empty file)"
        start = max(1, int(start_line)) if start_line else 1
        end = min(int(end_line), total) if end_line else total
        if start > total:
            raise ToolError(f"start_line {start} is beyond the end of the file ({total} lines)")
        capped = min(end, start + MAX_READ_LINES - 1)
        out = [f"{self._rel(p)} (lines {start}-{capped} of {total})"]
        out += [f"{i:>5}| {lines[i - 1]}" for i in range(start, capped + 1)]
        if capped < end:
            out.append(f"[truncated at {MAX_READ_LINES} lines. Call read_file again with start_line={capped + 1} to continue]")
        return "\n".join(out)

    def _t_search_code(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        base = resolve_in_sandbox(self.root, path)
        if base.is_file():
            files = [base]
        else:
            files = []
            for dp, dns, fns in os.walk(base):
                dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
                files += [Path(dp) / f for f in sorted(fns)]
        matches: list[str] = []
        for fp in files:
            if glob and not fnmatch.fnmatch(fp.name, glob):
                continue
            rp = real(fp)
            if not is_inside(self.root, rp) or is_secret_path(rp) or not rp.is_file():
                continue
            try:
                if rp.stat().st_size > MAX_READ_BYTES:
                    continue
                data = rp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{self._rel(rp)}:{n}: {line.strip()[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        matches.append(f"[stopped at {MAX_MATCHES} matches; narrow the pattern, path or glob]")
                        return "\n".join(matches)
        return "\n".join(matches) or "No matches"

    # ------------------------------------------------------------ write tools
    def _register_change(self, p: Path) -> None:
        if p not in self.changed and len(self.changed) >= self.cfg.max_files_changed:
            if not self.confirm(f"Agent wants to modify file #{len(self.changed) + 1} ({self._rel(p)}); "
                                f"the per-run limit is {self.cfg.max_files_changed}. Allow?"):
                raise GuardError("user declined modifying more files than the per-run limit")
        self.changed.add(p)

    def _t_edit_file(self, path: str, old_str: str, new_str: str) -> str:
        p = resolve_in_sandbox(self.root, path)
        check_writable(self.root, p)
        if not p.is_file():
            raise ToolError(f"'{path}' does not exist; use write_file to create it")
        if not old_str:
            raise ToolError("old_str must not be empty")
        text = p.read_bytes().decode("utf-8")
        n = text.count(old_str)
        if n == 0:
            raise ToolError("old_str was not found. It must match the file exactly, including whitespace. "
                            "Use read_file to check the current content.")
        if n > 1:
            raise ToolError(f"old_str appears {n} times; include more surrounding lines so it matches exactly once")
        self._register_change(p)
        new_text = text.replace(old_str, new_str, 1)
        p.write_bytes(new_text.encode("utf-8"))
        diff = list(difflib.unified_diff(text.splitlines(), new_text.splitlines(),
                                         f"a/{self._rel(p)}", f"b/{self._rel(p)}", lineterm="", n=2))
        shown = "\n".join(diff[:40]) + (f"\n[... {len(diff) - 40} more diff lines]" if len(diff) > 40 else "")
        return f"Edited {self._rel(p)}\n{shown}"

    def _t_write_file(self, path: str, content: str, overwrite: bool = False) -> str:
        p = resolve_in_sandbox(self.root, path)
        check_writable(self.root, p)
        if p.is_dir():
            raise ToolError(f"'{path}' is a directory")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError(f"content is larger than {MAX_WRITE_BYTES} bytes")
        if p.exists():
            if not overwrite:
                raise ToolError("file already exists. Use edit_file for small changes, or pass overwrite=true "
                                "to replace it entirely (asks the user for confirmation).")
            if not self.confirm(f"Agent wants to OVERWRITE {self._rel(p)}. Allow?"):
                raise GuardError("user declined the overwrite")
        self._register_change(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
        return f"Wrote {self._rel(p)} ({len(content.splitlines())} lines)"

    # ------------------------------------------------------------ commands
    def _t_run_command(self, command: str, timeout_s=None) -> str:
        argv = parse_command(self.root, command)
        if argv[0] in ("python", "python3"):
            argv[0] = sys.executable
        elif argv[0] == "pytest":
            argv = [sys.executable, "-m", "pytest"] + argv[1:]
        timeout = max(1, min(int(timeout_s or self.cfg.command_timeout_default), self.cfg.command_timeout_max))
        try:
            proc = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, errors="replace",
                                  timeout=timeout, env=scrubbed_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or b"")
            partial = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
            raise ToolError(_truncate(f"Command timed out after {timeout}s and was killed.\n--- partial stdout ---\n{partial}"))
        except FileNotFoundError:
            raise ToolError(f"command not found: {argv[0]}")
        return _truncate(f"exit code: {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
