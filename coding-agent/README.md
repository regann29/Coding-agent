# coding-agent

A small, readable, sandboxed AI coding agent. You give it a task in plain English; it explores a project
folder, edits files, runs tests, and iterates until it is done or hits a limit. About 600 lines of Python,
no agent framework, so every safety rule is visible in the code.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...          # your key
export AGENT_MODEL=...                # a current model id from the Anthropic docs (model names change)
python agent.py "fix the failing tests" --root ./path/to/project
```

Try it on a throwaway copy of a project first. The agent edits real files inside `--root`.

Options: `--max-steps N`, `--token-budget N`, `--yes` (auto-approve confirmation prompts).
Env vars: `AGENT_MODEL` (required), `AGENT_MAX_STEPS` (25), `AGENT_TOKEN_BUDGET` (150000).
Exit code is 0 only if the agent finished with a report; 1 if it stopped on a limit or error.

## How it works

```
task -> agent loop -> model -> tool calls -> guard layer -> tool executors -> results -> model -> ...
Stops when: the model finishes, OR max steps, OR token budget, OR you press Ctrl+C
```

| File | Role |
|---|---|
| `agent.py` | The loop, budgets, logging, CLI |
| `tools.py` | 6 tools: `list_dir`, `read_file`, `search_code`, `edit_file`, `write_file`, `run_command` |
| `guards.py` | Path jail, secret-file blocking, command allowlist |
| `prompts.py` | System prompt (explore, plan, small change, verify, report) |
| `evals/` | 5 graded tasks with automatic checkers |
| `tests/` | 94 tests: guards, tools, loop (with a scripted model), and the eval checkers |

Every run writes a JSONL log to `runs/` with each model turn and tool call.

## Safety model

- All paths are resolved (symlinks followed) and must stay inside `--root`.
- Commands run with no shell, from an allowlist: `python script.py`, `python -m pytest`, `pytest`, `pip list/show/freeze`,
  `git status/diff/log/show`, `ls`, `cat`, `grep`, `find` (no `-delete`/`-exec`). Pipes, redirects, `;`, `&`, backticks are refused.
- No writes to dotfiles, hidden folders, `.git/`, or secret-like files (`.env`, `*.pem`, `*.key`...). Those files can't be read either.
- Overwriting an existing file, or modifying a 6th distinct file in one run, asks you to confirm.
- Child processes get an environment with anything named like KEY/TOKEN/SECRET/PASSWORD removed.
- Step and token budgets stop runaway loops; a stopped run reports partial progress.

## Limitations (read these)

1. **This is a policy layer, not a security boundary.** The agent can run Python scripts and tests, and code it writes
   can do anything your user account can. Do not point it at untrusted repos or tasks outside a container or VM. Docker is the natural v2 upgrade.
2. Grep-style patterns containing `|`, `&`, or `>` are refused by the shell-operator check. The agent has `search_code` for regex searches.
3. No network tools, no git writes (commit/push), no package installs. By design for v1.
4. Extended thinking is off. If you turn it on, thinking blocks must be preserved in the message history (see `agent.py`).
5. Quality depends on the model. The eval suite exists so you can measure it instead of trusting a demo.
6. Large repos: files over 1 MB are skipped, reads are capped at 400 lines, command output at 8 KB.

## Testing

```bash
python -m pytest -q                   # 94 offline tests, no API key needed
python evals/run_evals.py             # 5 real-model tasks; costs tokens; writes evals/results.json
python evals/run_evals.py --only 2,5
```

The evals check outcomes, not just "did it say done": task 4 mutates the implementation to prove the agent's tests
catch bugs, and task 5 rejects a patch to the symptom instead of the root cause.

## Example run

Real loop and real tools, **scripted model** (`python examples/offline_demo.py`). It shows the shape of a run,
not how well a live model performs.

```

[step 1] Plan: run the tests, read the failing code, fix it, re-run.
  -> run_command({"command": "pytest -q"}) exit code: 1
  -> read_file({"path": "lists.py"}) lists.py (lines 1-2 of 2)

[step 3] The slice uses n - 1, so it drops one element. Changing it to n.
  -> edit_file({"path": "lists.py", "old_str": "nums[:n - 1]", "new_str": "nums[:n]"}) Edited lists.py
  -> run_command({"command": "pytest -q"}) exit code: 0

[step 5] Fixed. Changed lists.py: sum_first_n now sums nums[:n] (was nums[:n - 1]). pytest: all 4 tests pass. Nothing unresolved.

=== COMPLETED | steps=5 tokens=600 guard_blocks=0 ===
Fixed. Changed lists.py: sum_first_n now sums nums[:n] (was nums[:n - 1]). pytest: all 4 tests pass. Nothing unresolved.
```

## Changelog vs. the Design Spec

- **Weaker sandbox claim.** The spec listed an allowlist as the sandbox. Because `python` and `pytest` can execute arbitrary code, this is documented as a policy layer, not a boundary (limitation 1).
- **Shell-operator check is on the raw string**, so `|` inside a quoted grep pattern is refused (limitation 2).
- **Added:** `python -c`/stdin code refused; `pytest -p/--basetemp/--rootdir` refused; `git --output/-c/--ext-diff` refused; `~` paths refused; symlink arguments checked; `.env.*` treated as secret.
- **Added:** `python`/`pytest` run via the current interpreter so the same environment (and pytest) is used.
- **Added:** if the model's reply is cut off by the output limit, it is discarded and the model is asked for smaller steps.
- **Confirmation trigger:** the spec said "more than 5 files"; implemented as: the 6th distinct file asks, and each further new file asks again.
- **Example run** uses a scripted model (no API key was available while building), so live-model results are untested.
