# culprit

When a Claude Code task fails, culprit tells you what to change — your prompt, your environment, your
agent setup (CLAUDE.md, rules, skills), the harness (tools, MCP, settings, context) or the model — by
re-running the task with one of those changed at a time and seeing which change makes it pass.

It does not ask a model to guess the cause. A cause is proven only when the changed run passes clearly
more often than the unchanged one (one-sided Fisher exact test, p < 0.05 over 5 runs each).

## How a diagnosis works

1. `triage` splits your Claude Code logs (`~/.claude/projects/*.jsonl`) into episodes — one prompt and
   everything until your next prompt — and ranks the likely failures.
2. You `confirm` what went wrong: `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful`,
   or `no-failure`.
3. `criterion draft` writes what success means from your follow-up messages; you edit and `approve` it.
4. `restore` rebuilds the files the episode started from in a throwaway copy.
5. `baseline` replays the episode unchanged 5 times. Passing 3+ times means the failure was luck.
6. `diagnose` replays it with one layer improved at a time, cheapest fix first:

   | Change | Verdict if it now passes |
   |---|---|
   | prompt restated clearly | **task** — change how you ask |
   | your fix script runs first | **environment** — fix the repo or infrastructure |
   | better standing instructions | **agent-setup** — change CLAUDE.md, rules, skills |
   | all tools, relevant files preloaded, double budget | **harness** — change tools, MCP, settings |
   | Fable instead of the original model | **model** — change or route the model |

   If no single change works, it tries them together and in pairs: **multi-cause** or **unexplained**.
7. `report` ranks the changes by how many of your failures each would have fixed.

## Install

Requires macOS (APFS clones), Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and the `claude` CLI
logged in.

```bash
uv sync
uv run pytest -q
```

## Use

```bash
uv run culprit triage --top 40
uv run culprit confirm <episode-id> wrong-result
uv run culprit criterion draft <episode-id>     # edit runs/<episode>/criterion.json if needed
uv run culprit criterion approve <episode-id>
uv run culprit restore <episode-id>
uv run culprit baseline <episode-id> --n 1 --budget 3   # one cheap run first; read runs/<episode>/replays/
uv run culprit diagnose <episode-id>
uv run culprit report                           # writes runs/report.md
```

Batch: `uv run culprit diagnose-all --per-episode 40 --total 400` diagnoses every prepared episode.

Trust: label replays with `culprit review` / `culprit label <run> pass|fail`; `culprit calibration`
shows how often the judge agrees with you (calibrated at ≥ 20 labels and Cohen's kappa ≥ 0.6).
`culprit selftest <episode-id>` plants a known fault in each layer of an episode that normally passes
and reports how often culprit names it.

## Cost

Each replay is capped at $2; a full diagnosis is about 30 replays, capped at $40 per episode by
default. Judge and drafting calls are capped at $0.50 each.

## Safety

- Replays run in a copy placed beside your project (`.culprit-replay-<id>`), deleted afterwards.
- No MCP server connects, so Slack, Gmail and similar are unreachable.
- Git remotes are stripped from the copy; `git push` and `gh` are denied; git cannot climb out of the
  copy into a parent repository. Episodes in a git worktree or submodule are refused.
- Nothing is written to your original project; `runs/` is gitignored and never committed.

## Limits

- Episodes that used an MCP tool or asked you a question are not replayed.
- Files Claude Code never recorded are restored as they are today, not as they were.
- Replays start fresh: the original project's auto-memory is not loaded.
- A harness verdict says what made it pass, not whether the fix is yours or Claude Code's.

## Status

Built and unit-tested (146 tests). Not yet run end to end on real episodes. A final review's safety and
budget findings are being fixed; run paid diagnoses only after they land.

Design: [`docs/specs/2026-10-11-culprit-design.md`](docs/specs/2026-10-11-culprit-design.md) ·
Plan: [`docs/plans/2026-10-11-culprit-plan.md`](docs/plans/2026-10-11-culprit-plan.md)
