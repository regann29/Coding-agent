from agent import run_agent
from config import Config
from fakes import FakeClient, reply, text, tool


def setup(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return 1\n")
    return root


def run(root, replies, tmp_path, **cfg):
    client = FakeClient(replies)
    cfgo = Config(model="fake", **cfg)
    res = run_agent("do the thing", root, cfgo, confirm=lambda p: True, client=client,
                    verbose=False, log_dir=tmp_path / "logs")
    return res, client


def test_happy_path(tmp_path):
    root = setup(tmp_path)
    res, client = run(root, [
        reply(text("Plan: read then edit."), tool("t1", "read_file", path="a.py")),
        reply(tool("t2", "edit_file", path="a.py", old_str="return 1", new_str="return 2")),
        reply(text("Done. Changed a.py."), stop="end_turn"),
    ], tmp_path)
    assert res.status == "completed" and res.steps == 3 and res.files_changed == ["a.py"]
    assert "return 2" in (root / "a.py").read_text()
    assert res.total_tokens == 360 and res.final_text.startswith("Done")
    assert (tmp_path / "logs").glob("*.jsonl")


def test_multiple_tool_calls_returned_together(tmp_path):
    root = setup(tmp_path)
    res, client = run(root, [
        reply(tool("a", "list_dir"), tool("b", "read_file", path="a.py")),
        reply(text("ok"), stop="end_turn"),
    ], tmp_path)
    last_user = client.calls[1][-1]
    assert last_user["role"] == "user"
    assert [r["tool_use_id"] for r in last_user["content"]] == ["a", "b"]


def test_blocked_action_is_reported_and_loop_survives(tmp_path):
    root = setup(tmp_path)
    res, client = run(root, [
        reply(tool("x", "run_command", command="rm -rf .")),
        reply(tool("y", "read_file", path="../../etc/passwd")),
        reply(text("I could not do that."), stop="end_turn"),
    ], tmp_path)
    assert res.status == "completed" and res.guard_blocks == 2 and (root / "a.py").exists()
    result = client.calls[1][-1]["content"][0]
    assert result["is_error"] is True and "BLOCKED" in result["content"]


def test_step_limit_stops_endless_loop(tmp_path):
    root = setup(tmp_path)
    res, _ = run(root, [reply(tool(f"t{i}", "list_dir")) for i in range(30)], tmp_path, max_steps=5)
    assert res.status == "max_steps" and res.steps == 5 and "step limit" in res.final_text


def test_token_budget_stops(tmp_path):
    root = setup(tmp_path)
    res, _ = run(root, [reply(tool(f"t{i}", "list_dir"), tokens=(900, 100)) for i in range(30)],
                 tmp_path, token_budget=2500)
    assert res.status == "token_budget" and res.steps == 3


def test_max_tokens_reply_is_discarded_and_retried(tmp_path):
    root = setup(tmp_path)
    res, client = run(root, [
        reply(text("partial..."), tool("z", "write_file", path="b.py", content="tru"), stop="max_tokens"),
        reply(text("ok"), stop="end_turn"),
    ], tmp_path)
    assert res.status == "completed" and not (root / "b.py").exists()
    assert "discarded" in client.calls[1][-1]["content"]


def test_api_error_is_reported(tmp_path):
    root = setup(tmp_path)
    res, _ = run(root, [], tmp_path)     # fake raises RuntimeError on first call
    assert res.status == "error" and "out of scripted replies" in res.final_text


def test_requires_model(tmp_path):
    import pytest
    root = setup(tmp_path)
    with pytest.raises(ValueError):
        run_agent("x", root, Config(model=""), client=FakeClient([]))
