"""Runs the REAL agent loop and REAL tools on eval task 2 (off-by-one bug),
but with a SCRIPTED model instead of the API. No API key or network needed.
It shows what a run looks like; it does not show how well a real model performs."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from agent import run_agent                       # noqa: E402
from config import Config                         # noqa: E402
from evals.run_evals import materialize           # noqa: E402
from evals.tasks import TASKS                     # noqa: E402
from fakes import FakeClient, reply, text, tool   # noqa: E402

task = TASKS[1]
proj = materialize(task["files"])
script = [
    reply(text("Plan: run the tests, read the failing code, fix it, re-run."), tool("1", "run_command", command="pytest -q")),
    reply(tool("2", "read_file", path="lists.py")),
    reply(text("The slice uses n - 1, so it drops one element. Changing it to n."),
          tool("3", "edit_file", path="lists.py", old_str="nums[:n - 1]", new_str="nums[:n]")),
    reply(tool("4", "run_command", command="pytest -q")),
    reply(text("Fixed. Changed lists.py: sum_first_n now sums nums[:n] (was nums[:n - 1]). "
               "pytest: all 4 tests pass. Nothing unresolved."), stop="end_turn"),
]
run_agent(task["prompt"], proj, Config(model="scripted-demo"), confirm=lambda _p: True,
          client=FakeClient(script), log_dir=Path(tempfile.mkdtemp()))
print("\nChecker verdict:", task["check"](proj))
