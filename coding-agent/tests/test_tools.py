import pytest
from config import Config
from tools import Tools


@pytest.fixture
def proj(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "a.py").write_text("def f():\n    return 1\n\ndef g():\n    return 1\n")
    (r / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
    (r / ".env").write_text("SECRET=1")
    (r / "bin.dat").write_bytes(b"\x00\x01\x02")
    return r


def make(proj, confirm=lambda p: True, **cfg):
    return Tools(proj, Config(model="x", **cfg), confirm)


def test_list_and_read(proj):
    t = make(proj)
    out, err = t.execute("list_dir", {})
    assert not err and "a.py" in out
    out, err = t.execute("read_file", {"path": "a.py"})
    assert not err and "    1| def f():" in out


def test_read_truncates_with_instructions(proj):
    out, err = make(proj).execute("read_file", {"path": "big.txt"})
    assert not err and "start_line=401" in out and "line 400" in out and "line 401" not in out.split("[truncated")[0]
    out, _ = make(proj).execute("read_file", {"path": "big.txt", "start_line": 401, "end_line": 403})
    assert "line 401" in out and "line 404" not in out


def test_read_blocks_secret_binary_and_outside(proj):
    t = make(proj)
    assert t.execute("read_file", {"path": ".env"})[1] is True
    assert t.execute("read_file", {"path": "bin.dat"})[1] is True
    out, err = t.execute("read_file", {"path": "../../etc/passwd"})
    assert err and "BLOCKED" in out and t.guard_blocks == 2   # .env read + traversal are guard blocks; binary is a plain tool error


def test_search(proj):
    out, err = make(proj).execute("search_code", {"pattern": r"def \w+", "glob": "*.py"})
    assert not err and "a.py:1:" in out and "a.py:4:" in out
    assert make(proj).execute("search_code", {"pattern": "nomatch_zzz"})[0] == "No matches"
    assert make(proj).execute("search_code", {"pattern": "("})[1] is True
    assert "SECRET" not in make(proj).execute("search_code", {"pattern": "SECRET"})[0]


def test_edit_requires_unique_match(proj):
    t = make(proj)
    out, err = t.execute("edit_file", {"path": "a.py", "old_str": "return 1", "new_str": "return 2"})
    assert err and "2 times" in out
    out, err = t.execute("edit_file", {"path": "a.py", "old_str": "def f():\n    return 1", "new_str": "def f():\n    return 2"})
    assert not err and "+    return 2" in out
    assert (proj / "a.py").read_text().count("return 2") == 1
    assert t.execute("edit_file", {"path": "a.py", "old_str": "zzz", "new_str": "y"})[1] is True


def test_edit_preserves_crlf(proj):
    (proj / "c.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    make(proj).execute("edit_file", {"path": "c.py", "old_str": "a = 1", "new_str": "a = 9"})
    assert (proj / "c.py").read_bytes() == b"a = 9\r\nb = 2\r\n"


def test_write_file_overwrite_rules(proj):
    t = make(proj)
    assert not t.execute("write_file", {"path": "new/x.py", "content": "hi\n"})[1]
    assert (proj / "new" / "x.py").read_text() == "hi\n"
    out, err = t.execute("write_file", {"path": "a.py", "content": "z"})
    assert err and "overwrite" in out and "def f" in (proj / "a.py").read_text()
    # overwrite asks for confirmation
    denied = make(proj, confirm=lambda p: False)
    out, err = denied.execute("write_file", {"path": "a.py", "content": "z", "overwrite": True})
    assert err and "BLOCKED" in out and "def f" in (proj / "a.py").read_text()
    assert not t.execute("write_file", {"path": "a.py", "content": "z\n", "overwrite": True})[1]


def test_cannot_write_dotfiles_or_outside(proj):
    t = make(proj)
    for p in [".git/config", ".env", "../evil.py", "/tmp/evil.py"]:
        out, err = t.execute("write_file", {"path": p, "content": "x"})
        assert err and "BLOCKED" in out
    assert t.guard_blocks == 4


def test_sixth_file_needs_confirmation(proj):
    asked = []
    t = make(proj, confirm=lambda p: asked.append(p) or False)
    for i in range(5):
        assert not t.execute("write_file", {"path": f"f{i}.py", "content": "x"})[1]
    assert asked == []
    out, err = t.execute("write_file", {"path": "f5.py", "content": "x"})
    assert err and len(asked) == 1 and not (proj / "f5.py").exists()
    # re-editing an already-changed file does not prompt again
    assert not t.execute("edit_file", {"path": "f0.py", "old_str": "x", "new_str": "y"})[1]
    assert len(asked) == 1


def test_run_command(proj):
    t = make(proj)
    out, err = t.execute("run_command", {"command": "python a.py"})
    assert not err and "exit code: 0" in out
    out, err = t.execute("run_command", {"command": "rm -rf ."})
    assert err and "BLOCKED" in out and (proj / "a.py").exists()
    (proj / "boom.py").write_text("raise SystemExit(3)\n")
    out, err = t.execute("run_command", {"command": "python boom.py"})
    assert not err and "exit code: 3" in out         # non-zero exit is information, not a tool error


def test_command_timeout_and_no_secret_env(proj, monkeypatch):
    (proj / "slow.py").write_text("import time; time.sleep(30)\n")
    out, err = make(proj).execute("run_command", {"command": "python slow.py", "timeout_s": 1})
    assert err and "timed out" in out
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    (proj / "env.py").write_text("import os; print('LEAK' if 'ANTHROPIC_API_KEY' in os.environ else 'CLEAN')\n")
    assert "CLEAN" in make(proj).execute("run_command", {"command": "python env.py"})[0]


def test_unknown_tool_and_bad_args(proj):
    t = make(proj)
    assert t.execute("delete_everything", {})[1] is True
    assert t.execute("read_file", {})[1] is True            # missing required arg
    assert t.execute("read_file", "notadict")[1] is True
