# culprit

When a Claude Code task fails, culprit tells you which layer to change: your prompt, your environment,
your agent setup (CLAUDE.md, rules, skills), the harness (tools, MCP, settings, context) or the model.
It re-runs the task with one layer changed at a time. A layer is the cause only if its runs pass
clearly more often than unchanged runs (10 each, one-sided Fisher exact test, p < 0.05). No model
guesses the cause.

## How it decides

1. **Triage.** Your logs (`~/.claude/projects/*.jsonl`) are split into episodes: one prompt and
   everything until your next prompt. Episodes are ranked by interrupts, tool errors, compactions and
   slow turns. A prompt that is only a slash command (`/compact`, `/model opus`) is never a candidate.
   Episodes that can't be replayed are listed last, with the reason.
2. **Confirm.** You say what went wrong (`stopped-short`, `wrong-result`, `ignored-instructions`,
   `wasteful` or `no-failure`) and approve a drafted success criterion.
3. **Baseline.** The episode's starting files are rebuilt in a throwaway copy and the episode is
   replayed unchanged 10 times. A follow-up prompt ("now do it on main") is replayed with its session's
   earlier conversation in place, so Claude knows what "it" is. If 6 or more pass, the failure was luck (**not-reproducible**).
4. **Oracles.** The episode is replayed with one layer improved at a time, cheapest first:

   | Change | Verdict if it now passes |
   |---|---|
   | prompt restated clearly | **task**: change how you ask |
   | your fix script runs first | **environment**: fix the repo or infrastructure |
   | better standing instructions | **agent-setup**: change CLAUDE.md, rules, skills |
   | all tools, relevant files preloaded, double budget | **harness**: change tools, MCP, settings |
   | Fable instead of the original model | **model**: change or route the model |

   The restated prompt and the standing instructions are drafted by a model that sees only what the agent
   could have known before the episode began. For a follow-up, the prompt drafter also sees the session's
   earlier prompts and replies (not tool output). The instructions drafter sees the CLAUDE.md files (and
   rules and imports) Claude Code loads at launch, read from the starting copy for the project, plus its
   top-level folder layout, and writes rules that fill their gaps. They are tested the way you would apply
   them: appended to the project's `CLAUDE.md` in the throwaway copy (never your project). The drafter also
   suggests where they belong (project `CLAUDE.md`, `CLAUDE.local.md`, a path-scoped rule, your user
   `CLAUDE.md` or managed policy); the report shows that as an untested suggestion beside the verdict.

   If no single change works, the changes are tried together and in pairs: **multi-cause**, or
   **unexplained** if nothing works. Each verdict shows its numbers, e.g.
   `harness: baseline 0/10 → 8/10, p = 0.0004`.
5. **Report.** The report starts with what to fix first: single-layer fixes ranked by how many failures
   each fixes. Then come layers that only work together, episodes left for your review, the
   symptom × cause grid, and how far to trust the results.

## Install

Needs macOS (APFS clones), Python ≥ 3.11, [uv](https://docs.astral.sh/uv/) and a logged-in `claude` CLI.

```bash
uv sync
uv run pytest -q
```

## Use

```bash
uv run culprit triage --top 40     # rank likely failures into the queue
uv run culprit confirm             # walk the queue: symptom, criterion, restore (needs a terminal)
uv run culprit diagnose 1          # one episode (queue row 1): baseline, oracles, verdict
uv run culprit diagnose            # every confirmed episode, then the report
uv run culprit report              # write runs/report.md
uv run culprit status [1]          # where episodes stand and the next command
```

- **Naming episodes:** use the triage row number (`3`), the short id (`1a2b3c4d:5e6f7a8b`), any unique prefix,
  or the full id.
- **Confirming one episode:** `confirm <episode> <symptom>` drafts the criterion. Edit
  `runs/<episode>/criterion.json` if it's wrong, then run the same command again to approve it.
- **Trust:** `review` asks you pass/fail on judged replays. The judge counts as calibrated at ≥ 20 labels
  and Cohen's kappa ≥ 0.6, and `status` and the report show its agreement. `selftest <episode>` plants a
  known fault in each layer of an episode that normally passes, and scores whether culprit names it.
- **Environment fix:** `env-fix <episode> <script>` supplies the script the environment oracle runs first.
- **Settings:** set `CULPRIT_RUNS` to change the store (default `./runs`) and `CULPRIT_PROJECTS` to change
  the logs (default `~/.claude/projects`). `CULPRIT_CLAUDE_BIN` picks the Claude Code build for replays.
- **Output:** results go to stdout. Progress (one line per replay, plus a heartbeat every 30 s) and
  `next:` hints go to stderr. `status --json` prints one JSON document.
- **Exit codes:** 0 done · 1 refused or failed · 2 bad arguments · 3 a prior step is missing · 4 spend
  cap reached · 130 Ctrl-C. Finished replays are kept, so rerunning the command resumes.

## Cost

| Cap | Value |
|---|---|
| Per replay | 3× what the original episode cost, at least $10 (harness variants twice that) |
| Per replay, time | 3× how long the original episode took, at least 30 minutes |
| Per judge or drafting call | $0.50, charged at the cap |
| Per episode | the worst case at its per-replay cap: $3,600 at the $10 floor |
| Per selftest fault | the same for a planted fault: $2,650 at the $10 floor |
| `diagnose` with no id, in total | $72,000 (20 episodes at the floor; each keeps its own cap inside it) |

- **The episode's own cost:** read from the session's running total (`cost-state` records, `totalCostUSD`) at
  the end of the episode less the one before it. Claude Code writes those rarely (about 1.5% of episodes in
  one real set of logs), so most episodes get the $10 floor. Its duration comes from its `turn_duration`
  records (or its first and last timestamps). The plan shows the per-replay cap.

- **Typical diagnosis:** about 60 replays. The episode and selftest caps cover the worst case (every
  variant, every retry), so they never cut a diagnosis short.
- **What caps count:** caps include money already spent, so resuming never double-spends.
- **Plan printed first:** before spending, culprit prints the most it could spend.
- **Void runs:** only infrastructure failures (an API error or overload, a lost connection, the CLI crashing
  or failing to start) are void: they don't count and are retried.
- **Runs that hit a limit count:** a replay stopped by its spend cap or its time limit is scored on what it
  produced (it usually fails), and `status`, `diagnose` and the report say how many runs stopped at each limit.
- **Late-session episodes:** every replay re-sends the session's earlier conversation, so they cost more
  per run and are likelier to hit the per-replay cap (and then fail that run).
- **Not testable:** if no result could beat the baseline, culprit stops before running any oracles.

## Safety

- Replays run in a copy beside your project (`.culprit-replay-<id>`), deleted afterwards. A follow-up's
  earlier conversation is a copy of your session log under `~/.claude/projects/`, also deleted afterwards;
  your own log is only read. File tools are
  denied on your project, and culprit stops if your project changes during a replay.
- Shell commands run in Claude Code's OS sandbox and can write only to the copy and the temp folder.
- Git remotes are stripped, and `git push` and `gh` are denied. Worktrees and submodules are refused.
- MCP servers never connect. A stand-in answers with the results recorded in your log.
- Episodes in your home folder, in a folder containing culprit, or under an unwritable parent are refused.
- `runs/` holds your session content and is gitignored.

## Limits

- **Files:** restored from Claude Code's snapshots and the episode's own edits. In git, everything else
  is rolled back to the last commit before the episode. Outside git, unrecorded files are as they are
  today.
- **Memory:** Claude's project memory is today's copy, not the copy from the time of the episode.
- **Questions Claude asked you:** answered with your recorded answer if the replay asks something similar
  (similarity ≥ 0.6). Anything else is refused.
- **MCP calls:** a call the original session never made fails.
- **Images:** an image the log didn't keep blocks the replay.
- **Starting point:** replays start at the beginning of the episode, with the session's earlier
  conversation in place (a fork of your log cut just before your prompt), not at the failing step.
- **Harness verdicts:** don't say whether the fix is yours or Claude Code's. Replaying on another build
  (`CULPRIT_CLAUDE_BIN`) narrows it.

## Status

Built and unit-tested. Triage and restore have run on real logs. No real episode has been replayed yet,
so start with one `diagnose` and read its output.

Design: [`docs/specs/2026-10-11-culprit-design.md`](docs/specs/2026-10-11-culprit-design.md) ·
Plan: [`docs/plans/2026-10-11-culprit-plan.md`](docs/plans/2026-10-11-culprit-plan.md)
