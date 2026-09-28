import os
import pytest
from guards import GuardError, resolve_in_sandbox, check_writable, parse_command, is_secret_path, scrubbed_env


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    return r


# ---- paths
def test_relative_path_ok(root):
    assert resolve_in_sandbox(root, "a.py") == (root / "a.py").resolve()


@pytest.mark.parametrize("p", ["../x", "../../etc/passwd", "/etc/passwd", "sub/../../x"])
def test_traversal_blocked(root, p):
    with pytest.raises(GuardError):
        resolve_in_sandbox(root, p)


def test_symlink_escape_blocked(root, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    os.symlink(outside, root / "link")
    with pytest.raises(GuardError):
        resolve_in_sandbox(root, "link")


@pytest.mark.parametrize("p", ["", "  ", None])
def test_empty_path_blocked(root, p):
    with pytest.raises(GuardError):
        resolve_in_sandbox(root, p)


def test_dotfiles_and_git_not_writable(root):
    for p in [".env", ".git/config", ".hidden/x.py", "sub/.secret"]:
        with pytest.raises(GuardError):
            check_writable(root, resolve_in_sandbox(root, p))
    check_writable(root, resolve_in_sandbox(root, "sub/new.py"))  # fine


def test_secret_detection():
    for n in [".env", ".env.local", "id_rsa", "server.pem", "api.key"]:
        assert is_secret_path(n)
    assert not is_secret_path("main.py")


# ---- commands
@pytest.mark.parametrize("cmd", [
    "pytest -q", "pytest tests/ -k foo", "python -m pytest -q", "python3 a.py", "python --version",
    "git status", "git diff", "git log --oneline", "ls", "ls -la .", "cat a.py", "grep -n x a.py",
    "find . -name '*.py'", "pip list",
])
def test_allowed_commands(root, cmd):
    assert parse_command(root, cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf .", "rm a.py", "sudo ls", "curl http://x.com", "wget http://x.com", "bash -c ls",
    "ls; rm a.py", "ls && rm a.py", "ls | cat", "cat a.py > b.py", "echo hi", "ls `whoami`", "ls $(whoami)",
    "python -c 'import os'", "python", "python -m http.server", "python -m pip install x",
    "git push", "git commit -m x", "git checkout .", "git diff --output=x.txt", "git -c core.pager=x log",
    "find . -delete", "find . -exec rm {} ;", "pip install requests", "pytest -p evil",
    "pytest --basetemp=/tmp/x", "cat ../x", "cat /etc/passwd", "cat ~/.bashrc", "cat .env", "ls ..",
    "", "   ", "cat 'unterminated",
])
def test_blocked_commands(root, cmd):
    with pytest.raises(GuardError):
        parse_command(root, cmd)


def test_symlink_arg_blocked(root, tmp_path):
    (tmp_path / "out.txt").write_text("x")
    os.symlink(tmp_path / "out.txt", root / "sneaky")
    with pytest.raises(GuardError):
        parse_command(root, "cat sneaky")


def test_env_scrubbed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "t")
    env = scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env and "MY_TOKEN" not in env and "PATH" in env
