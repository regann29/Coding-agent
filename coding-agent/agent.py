"""The agent loop + CLI.

Usage:  python agent.py "fix the failing test" --root ./my_project
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import Config
from guards import real
from prompts import build_system_prompt
from tools import TOOL_SCHEMAS, Tools

DEFAULT_LOG_DIR = Path(__file__).parent / "runs"


@dataclass
class RunResult:
    status: str                 # completed | max_steps | token_budget | aborted | error | stopped:<reason>
    steps: int
    total_tokens: int
    final_text: str
    guard_blocks: int
    files_changed: list = field(default_factory=list)
    log_path: str = ""


def cli_confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print(f"[confirm] {prompt} -> denied (no interactive terminal; use --yes to auto-approve)")
        return False
    return input(f"\n[confirm] {prompt} [y/N] ").strip().lower() in ("y", "yes")


def _block_to_param(b) -> dict | None:
    """Convert an SDK content block into a plain dict we send back next turn."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return None  # thinking is off in v1; ignore unknown block types


class RunLog:
    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.jsonl"
        self._f = open(self.path, "a", encoding="utf-8")

    def write(self, event: str, **data):
        self._f.write(json.dumps({"t": round(time.time(), 3), "event": event, **data}, default=str) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()


def run_agent(task: str, root, cfg: Config, confirm: Callable[[str], bool] = cli_confirm,
              client=None, verbose: bool = True, log_dir: Path | None = None) -> RunResult:
    root = real(root)
    if not root.is_dir():
        raise ValueError(f"root '{root}' is not a directory")
    if not cfg.model:
        raise ValueError("No model set. Export AGENT_MODEL (see README) before running.")
    if client is None:
        import anthropic  # imported lazily so unit tests don't need the package
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    tools = Tools(root, cfg, confirm)
    system = build_system_prompt(root)
    log = RunLog(log_dir or DEFAULT_LOG_DIR)
    log.write("start", task=task, root=str(root), model=cfg.model, max_steps=cfg.max_steps, token_budget=cfg.token_budget)

    messages: list[dict] = [{"role": "user", "content": task}]
    steps, total, status, final_text = 0, 0, "max_steps", ""

    def say(msg: str):
        if verbose:
            print(msg, flush=True)

    try:
        while steps < cfg.max_steps:
            if total >= cfg.token_budget:
                status = "token_budget"
                break
            steps += 1
            resp = client.messages.create(model=cfg.model, max_tokens=cfg.max_output_tokens,
                                          system=system, tools=TOOL_SCHEMAS, messages=messages)
            total += resp.usage.input_tokens + resp.usage.output_tokens
            blocks = [p for p in (_block_to_param(b) for b in resp.content) if p]
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
            log.write("model", step=steps, stop_reason=resp.stop_reason, tokens=total, content=blocks)
            if text:
                say(f"\n[step {steps}] {text}")

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": blocks})
                results = []
                for b in blocks:
                    if b["type"] != "tool_use":
                        continue
                    out, is_err = tools.execute(b["name"], b["input"])
                    log.write("tool", step=steps, name=b["name"], input=b["input"], is_error=is_err, output=out)
                    say(f"  -> {b['name']}({json.dumps(b['input'])[:120]}) {'ERROR: ' if is_err else ''}{out.splitlines()[0][:100] if out else ''}")
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
            elif resp.stop_reason == "end_turn":
                messages.append({"role": "assistant", "content": blocks})
                status, final_text = "completed", text
                break
            elif resp.stop_reason == "max_tokens":
                # Output was cut off; a truncated tool call is unreliable, so discard it and ask for smaller steps.
                messages.append({"role": "user", "content": "Your last response hit the output limit and was discarded. "
                                                            "Continue with smaller steps (e.g. smaller edits)."})
            else:
                status, final_text = f"stopped:{resp.stop_reason}", text
                break
    except KeyboardInterrupt:
        status = "aborted"
    except Exception as e:
        status, final_text = "error", f"{type(e).__name__}: {e}"
        log.write("error", error=final_text)

    changed = sorted(str(p.relative_to(root)) for p in tools.changed)
    if status != "completed" and not final_text:
        final_text = {"max_steps": f"Stopped: reached the step limit ({cfg.max_steps}).",
                      "token_budget": f"Stopped: reached the token budget ({cfg.token_budget}).",
                      "aborted": "Stopped: aborted by user."}.get(status, f"Stopped: {status}")
        final_text += f" Files changed so far: {', '.join(changed) or 'none'}. The task may be incomplete."
    log.write("end", status=status, steps=steps, tokens=total, files_changed=changed, guard_blocks=tools.guard_blocks)
    log.close()
    say(f"\n=== {status.upper()} | steps={steps} tokens={total} guard_blocks={tools.guard_blocks} ===\n{final_text}\nLog: {log.path}")
    return RunResult(status, steps, total, final_text, tools.guard_blocks, changed, str(log.path))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A small sandboxed coding agent.")
    ap.add_argument("task", help="What you want done, in plain English")
    ap.add_argument("--root", default=".", help="Project directory the agent may read/modify (default: current dir)")
    ap.add_argument("--max-steps", type=int, help="Override AGENT_MAX_STEPS")
    ap.add_argument("--token-budget", type=int, help="Override AGENT_TOKEN_BUDGET")
    ap.add_argument("--yes", action="store_true", help="Auto-approve confirmation prompts (overwrites, >5 files)")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.max_steps:
        cfg.max_steps = args.max_steps
    if args.token_budget:
        cfg.token_budget = args.token_budget
    try:
        res = run_agent(args.task, args.root, cfg, confirm=(lambda _p: True) if args.yes else cli_confirm)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0 if res.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
