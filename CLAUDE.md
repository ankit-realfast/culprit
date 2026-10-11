# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

culprit diagnoses why a Claude Code episode failed by **intervention**: it replays the episode through
headless `claude -p` with one layer changed at a time (task, environment, agent setup, harness, model)
and reports which change makes it pass. Input is the user's own session logs in
`~/.claude/projects/*.jsonl`; everything runs locally.

**Current state:** built and unit-tested (`uv run pytest -q`); not yet run end to end on a paid replay. The
source of truth is:

- `docs/specs/2026-10-11-culprit-design.md` — the taxonomy (symptom × cause axes), architecture, validity guards, known limits.
- the code itself.

`docs/plans/2026-10-11-culprit-plan.md` is the historical build plan: its exact code, paths and commands
predate the current CLI and module layout. If code and spec disagree, the spec wins; change the spec
deliberately rather than drifting from it.

## Commands

```bash
uv sync                                   # install (pytest is the only dev dependency)
uv run pytest -v                          # all tests
uv run pytest tests/test_episodes.py -v   # one file
uv run pytest tests/test_episodes.py::test_a_partial_last_line_is_skipped   # one test
uv run culprit --help                     # commands: triage, confirm, diagnose, report, status,
                                          # env-fix, review, selftest
```

The CLI keeps flags to `triage --top` and `status --json`; the store and the logs come from `CULPRIT_RUNS`
(default `runs/`) and `CULPRIT_PROJECTS` (default `~/.claude/projects`). Episodes can be named by row number (`3`; never `#3`, which shells treat as a comment),
short id or unique prefix. Don't add flags or subcommands without the user asking.

## Architecture

Stdlib-only Python ≥3.11 package under `src/culprit/`, one module per pipeline stage:

- `logs` — episodes, failure signals, triage ranking, locating an episode, snapshots, replay eligibility
- `store` — the taxonomy and the plain-file store under `runs/`
- `stubs` — the stand-in MCP servers, each its own light process (`python -m culprit.stubs mcp|answers …`);
  imports nothing beyond the standard library and `logs`
- `replay` — restoring the starting files and running one locked-down replay
- `model` — every model call (`ask_claude`), criterion drafting, the leak check, the judge
- `diagnose` — variants, oracles, verdict and its Fisher test, the cost preview, the whole-queue batch
- `trust` — judge calibration and selftest
- `report` — the report and per-episode status
- `cli` — the command line and its progress lines

Each module imports only modules above it in this list; keep it that way (by where code lives) rather
than adding function-level imports. Pipeline per episode:

episodes → triage (human confirms symptom) → criterion (human approves success criterion) → restore
(starting files rebuilt in a throwaway dir) → baseline (N=10 unchanged replays, judged) → oracles
(one variant per cause, leak-checked) → verdict (Fisher test, first match wins, then pair test) →
report (symptom × cause grid, ranked fix list). Calibration and selftest measure culprit's own trust.

Key cross-cutting rules:

- **Episode** = one human prompt plus every record after it until the next human prompt, id `<sessionId>:<promptId>`. Episodes are cut by **file order**, not by grouping on `promptId` (only `user` records carry it). Injected markup (`<task-notification>`, `<command-name>`, …) and interrupts do not open episodes.
- **Taxonomy values and order are fixed** (first match wins): symptoms `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful` (+ `no-failure`); causes `misjudged`, `not-reproducible`, `task`, `environment`, `agent-setup`, `harness`, `model`, `multi-cause`, `unexplained`.
- **Every model call** goes through `culprit.model.ask_claude` (no tools, empty strict MCP config, `--permission-prompts none`, `--no-session-persistence`, `--max-budget-usd`). Drafting/judge model `claude-sonnet-5-5`; oracle model `claude-fable-5-1`.
- **Every replay is locked down**: fresh APFS clone (`cp -cR`) beside the original cwd at `<parent>/.culprit-replay-<12 hex>` (so ancestor CLAUDE.md files load), git remotes removed, no MCP, `git push`/`gh` denied, spend-capped, stream-json output. Permission mode: `auto`/`acceptEdits` as recorded, anything else → `acceptEdits` (the CLI has no `default` mode). Clone and the empty `~/.claude/projects/<encoded>/memory/` folder are deleted afterwards.
- **The judge** decides from the criterion plus the final answer, the diff, the check results and the transcript's tool calls; a failing check or errored run fails without a model call.
- **Restores never write to the original working directory**; every tracked path must resolve inside the destination.
- **Each oracle may change only its own fields** (task → `prompt`; environment → `setup_script`; agent-setup → `claude_md_addition`, written into the replay copy's project CLAUDE.md; harness → `tools`, `disallowed_tools`, `context_preamble`, `max_budget_usd`; model → `model`).
- Storage is plain files under `runs/` (one folder per episode; `runs/selftest/` kept separate from real calibration).

## Hard constraints

- `runs/` is gitignored and holds client work: **real session content never enters the repo**. Tests use synthetic records (`tests/records.py`).
- Tests never call the real `claude` binary — inject a fake. Real calls happen only in each task's explicit manual step.
  `tests/conftest.py` enforces this (a failing stand-in `claude` first on PATH) and points every default at
  tmp paths, so no test reads real logs or writes the repo's `runs/`; keep both guards.
- No runtime dependencies beyond the standard library.
