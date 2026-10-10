# culprit — session handoff (2026-10-11)

Read this, then `CLAUDE.md`, `README.md`, and only the spec/plan sections you need. Repo:
`/Users/akp/Downloads/culprit`, branch `main`, pushed to `https://github.com/ankit-realfast/culprit`
at `427c2f7`. 199 tests pass (`uv run pytest -q`).

## What culprit is

It answers "when a Claude Code task fails, do I change the prompt, environment, agent setup, harness or
model?" by intervention, not judgement: it replays the failed episode from the user's own logs
(`~/.claude/projects/*.jsonl`) through headless `claude -p`, one layer improved at a time, and the
cheapest layer whose variant beats the unchanged baseline (one-sided Fisher exact, p < 0.05, N = 5)
is the verdict. Taxonomy, method and guards: `docs/specs/2026-10-11-culprit-design.md`. The original
build plan (26 tasks, all done): `docs/plans/2026-10-11-culprit-plan.md`.

## State

- **Built and unit-tested; never run end to end on a real paid replay.** Real-data runs so far: triage
  (5,189 episodes), restore on three real episodes (all agree), one criterion draft, several cheap live
  Haiku probes (each listed in the spec §2 / commit messages).
- **Ready to start:** episode `90042e20-39ce-4c83-83f3-8ec28733d50a:1e3a09ca-f204-4cfb-b638-9b88846f553f`
  (staffing-wiki) is restored (`runs/<id with : as __>/pristine`, `restore.json`) with a drafted, not yet
  approved criterion. Human steps next: `culprit confirm <id> <symptom>`, `culprit criterion approve <id>`,
  then `culprit baseline <id> --n 1 --budget 3` and read `runs/<episode>/replays/baseline/0/`.
- `runs/` (gitignored) holds client data. Never commit it.

## Module map (`src/culprit/`, one job each)

episodes (split logs) · signals · triage · taxonomy (Symptom, Cause, ORACLE_ORDER) · store · locate ·
claude (`ask_claude`, the only judge/draft model call) · criterion · snapshot (`load_snapshot`,
`session_root`) · eligibility (`replay_blockers`, `project_hooks`) · sandbox (restore: clone → snapshot
overlay → `restore_from_git` → `check_restore` → `apply_episode_originals`) · replay (`ReplaySettings`,
`replay_command`, `run_replay`, `run_in_own_process_group`) · mcp_stub (record/replay MCP stand-in +
shared JSON-RPC `serve_message`) · answer_host (permission-prompt tool answering AskUserQuestion) ·
judge · experiment (`run_variant`, `EpisodeBudget`, `stored_spend`) · stats · leak · oracles · verdict ·
diagnose (`diagnose`, `retire_if_criterion_changed`) · calibration · selftest · report · batch · cli.

## Decisions that are easy to undo by accident

- Restore: snapshot paths are relative to the **session's first cwd**; `backupFileName: null` = unknown,
  never "delete"; the episode's own first-touch records (`originalFile`, Write `create`) override the
  snapshot; git projects roll other files back to the last commit before the episode.
- Replays: clone beside the original cwd; `GIT_CEILING_DIRECTORIES`; git remotes stripped; OS Bash
  sandbox via `--settings` (`allowUnsandboxedCommands: false`); Edit/Write denied on the original;
  before/after fingerprint raises `OriginalModified`; prompt on stdin; process-group kill on timeout;
  bounded diff; copy of project memory via `autoMemoryDirectory`; MCP servers replaced by the stand-in;
  AskUserQuestion via `--permission-prompt-tool mcp__culprit_answers__answer` (a PreToolUse hook does
  **not** expose it on 2.1.296 — verified from the init tool list).
- Runs that fail outside the agent (API error, culprit's cap, timeout) are **void**: not counted, retried.
  A baseline no result could beat at N is reported "not testable" before any oracle spend.
- Budgets count everything already spent in the folder (`stored_spend`); `--total` spans all batches.
- Runs judged against an older criterion move to `superseded/<n>/` (spend still counted).

## Open items

- Deferred minors: no-failure episodes need criterion+restore before Misjudged; `is_control` ignores run
  count; selftest budget stop hides later fault lines; full-set multi-cause labelled "two layers";
  prompts starting with "<" merge into the previous episode; env-fix path relative to cwd.
- Remaining limits: an MCP call or question the original never made fails in replay; memory is today's
  copy; non-git untracked files are as today; replays start at the episode's beginning (fork from the
  failing step via `--resume --fork-session` is untested); yours-vs-vendor only narrowed via
  `CULPRIT_CLAUDE_BIN`.
- Untested routes: a user-level `~/.claude/settings.json` hook for AskUserQuestion; the Python Agent SDK
  `canUseTool`.

## How this user works

- Concise, no fluff; "eli10" means plain words. `/Users/akp/Downloads/CLAUDE.md` asks for answer-first
  (Pyramid) and MECE structure. One master doc, not many files.
- Verify before claiming: check docs/online and run a cheap live probe before calling something a limit.
- "Don't cut corners": TDD (failing test first), commit per change with a why-message ending in
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, ask before pushing.

## Gotchas from this session

- Never run `git checkout -- .` (or similar) in the repo during cleanup — it once wiped verified,
  uncommitted work.
- The Bash tool's shell is zsh: `$var` holding "flag value" is not word-split; wrap multi-flag probes in
  `bash <<'BASH' … BASH`.
- Live probes run from `mktemp -d` leave empty `~/.claude/projects/-private-var-folders-…/memory`
  folders; remove them with `rmdir` (never `rm -rf`). culprit's own replays clean up after themselves.
- Paid steps (baseline, diagnose, selftest, diagnose-all) write `.culprit-replay-*` beside the user's
  projects and spend money: ask the user first.
