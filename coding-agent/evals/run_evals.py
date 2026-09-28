"""Run the evaluation tasks against the real model.  Costs API tokens.

    python evals/run_evals.py             # all 5
    python evals/run_evals.py --only 2,5  # subset
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import run_agent          # noqa: E402
from config import Config            # noqa: E402
from evals.tasks import TASKS        # noqa: E402


def materialize(files: dict) -> Path:
    root = Path(tempfile.mkdtemp(prefix="agent-eval-"))
    for name, content in files.items():
        (root / name).write_text(content)
    return root


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated task ids, e.g. 2,5")
    args = ap.parse_args()
    wanted = {int(x) for x in args.only.split(",")} if args.only else None
    cfg = Config.from_env()

    rows = []
    for t in TASKS:
        if wanted and t["id"] not in wanted:
            continue
        root = materialize(t["files"])
        t0 = time.time()
        res = run_agent(t["prompt"], root, cfg, confirm=lambda _p: True, verbose=False)
        ok, note = t["check"](root)
        rows.append(dict(id=t["id"], name=t["name"], passed=ok and res.status == "completed", status=res.status,
                         steps=res.steps, tokens=res.total_tokens, seconds=round(time.time() - t0, 1),
                         guard_blocks=res.guard_blocks, note=note, log=res.log_path))
        print(f"[{'PASS' if rows[-1]['passed'] else 'FAIL'}] #{t['id']} {t['name']:<26} steps={res.steps:<3} tokens={res.total_tokens:<7} {note[:70]}")

    out = Path(__file__).parent / "results.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed. Details: {out}")


if __name__ == "__main__":
    main()
