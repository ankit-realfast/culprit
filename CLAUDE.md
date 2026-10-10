# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

culprit diagnoses why a Claude Code episode failed by **intervention**: it replays the episode through
headless `claude -p` with one layer changed at a time (task, environment, agent setup, harness, model)
and reports which change makes it pass. Input is the user's own session logs in
`~/.claude/projects/*.jsonl`; everything runs locally.

**Current state:** design and plan only — no code yet. The source of truth is:

- `docs/specs/2026-10-11-culprit-design.md` — the taxonomy (symptom × cause axes), architecture, validity guards, known limits.
- `docs/plans/2026-10-11-culprit-plan.md` — six stages, executed in order, each task written test-first with exact code, file paths and commands. Implement it task by task (the plan names `superpowers:subagent-driven-development` or `superpowers:executing-plans`). Each stage lists its own Global Constraints and Review Focus; read them before starting that stage.

If code and spec/plan disagree, the spec wins; change the spec deliberately rather than drifting from it.

## Commands (once Stage 1 Task 1.1 creates `pyproject.toml`)

```bash
uv sync                                   # install (pytest is the only dev dependency)
uv run pytest -v                          # all tests
uv run pytest tests/test_episodes.py -v   # one file
uv run pytest tests/test_episodes.py::test_a_partial_last_line_is_skipped   # one test
uv run culprit triage --top 20            # CLI entry point (later: confirm, criterion, restore,
                                          # baseline, env-fix, diagnose, review, label,
                                          # calibration, selftest, report, diagnose-all)
```

## Architecture (planned)

Stdlib-only Python ≥3.11 package under `src/culprit/`, one module per job. Pipeline per episode:

episodes → triage (human confirms symptom) → criterion (human approves success criterion) → restore
(starting files rebuilt in a throwaway dir) → baseline (N=5 unchanged replays, judged) → oracles
(one variant per cause, leak-checked) → verdict (Fisher test, first match wins, then pair test) →
report (symptom × cause grid, ranked fix list). Calibration and selftest measure culprit's own trust.

Key cross-cutting rules:

- **Episode** = one human prompt plus every record after it until the next human prompt, id `<sessionId>:<promptId>`. Episodes are cut by **file order**, not by grouping on `promptId` (only `user` records carry it). Injected markup (`<task-notification>`, `<command-name>`, …) and interrupts do not open episodes.
- **Taxonomy values and order are fixed** (first match wins): symptoms `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful` (+ `no-failure`); causes `misjudged`, `not-reproducible`, `task`, `environment`, `agent-setup`, `harness`, `model`, `multi-cause`, `unexplained`.
- **Every model call** goes through `culprit.claude.ask_claude` (no tools, empty strict MCP config, `--permission-prompts none`, `--no-session-persistence`, `--max-budget-usd`). Drafting/judge model `claude-sonnet-5-5`; oracle model `claude-fable-5-1`.
- **Every replay is locked down**: fresh APFS clone (`cp -cR`) beside the original cwd at `<parent>/.culprit-replay-<12 hex>` (so ancestor CLAUDE.md files load), git remotes removed, no MCP, `git push`/`gh` denied, spend-capped, stream-json output. Permission mode: `auto`/`acceptEdits` as recorded, anything else → `acceptEdits` (the CLI has no `default` mode). Clone and the empty `~/.claude/projects/<encoded>/memory/` folder are deleted afterwards.
- **The judge** decides from the criterion plus the final answer, the diff, the check results and the transcript's tool calls; a failing check or errored run fails without a model call.
- **Restores never write to the original working directory**; every tracked path must resolve inside the destination.
- **Each oracle may change only its own fields** (task → `prompt`; environment → `setup_script`; agent-setup → `append_system_prompt`; harness → `tools`, `disallowed_tools`, `context_preamble`, `max_budget_usd`; model → `model`).
- Storage is plain files under `runs/` (one folder per episode; `runs/selftest/` kept separate from real calibration).

## Hard constraints

- `runs/` is gitignored and holds client work: **real session content never enters the repo**. Tests use synthetic records (`tests/records.py`).
- Tests never call the real `claude` binary — inject a fake. Real calls happen only in each task's explicit manual step.
- No runtime dependencies beyond the standard library.
