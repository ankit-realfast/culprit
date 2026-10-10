# culprit

When a Claude Code task fails, culprit tells you what to change — your prompt, your environment, your
agent setup (CLAUDE.md, rules, skills), the harness (tools, MCP, settings, context) or the model — by
re-running the task with one of those changed at a time and seeing which change makes it pass.

It does not ask a model to guess the cause. A cause is proven only when the changed run passes clearly
more often than the unchanged one (one-sided Fisher exact test, p < 0.05 over 5 runs each).

## How a diagnosis works

1. `triage` splits your Claude Code logs (`~/.claude/projects/*.jsonl`) into episodes — one prompt and
   everything until your next prompt — and ranks the likely failures. The strongest sign is your next
   prompt correcting Claude ("no, still broken", "why are we…", "undo that"); the row shows it as
   `corrected: …`. Interrupts, tool errors, compactions and slow turns order the rest. Each run merges
   into the stored queue (`runs/candidates.json`), so episodes queued earlier are kept: the rows it prints become
   queue rows #1..#n, and earlier episodes it no longer shows follow them (`culprit status` lists all).
   Episodes a replay cannot use (say, their working directory is gone) come after every replayable one,
   with the reason printed under the row.
2. You `confirm` what went wrong: `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful`,
   or `no-failure`.
3. `criterion draft` writes what success means from your follow-up messages; you edit and `approve` it.
   Drafting again over an existing criterion needs `--force` (it replaces it with a new unapproved draft).
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
uv run culprit baseline <episode-id> --n 1   # one cheap run first; read runs/<episode>/replays/
uv run culprit diagnose <episode-id>
uv run culprit report                           # writes runs/report.md
uv run culprit status [<episode-id>]            # where each episode stands and what to run next
```

`culprit --help` lists the commands by group (workflow, human review, trust, batch) with this run-through;
`culprit <command> --help` ends with examples.

For scripts, `triage`, `status`, `baseline`, `diagnose` and `calibration` take `--json`: stdout is then
one JSON document and nothing else (progress, `next:` lines and questions stay on stderr).

`baseline`, `diagnose`, `selftest` and `diagnose-all` print a line on stderr as each replay finishes:
its result, its cost and what the episode has spent so far. While a replay runs, a
`… still running` line every 30 s shows it is alive.

Each command that changes an episode ends with a `next:` line (on stderr) naming the step that follows;
`culprit status` shows the same for every queued episode.

`<episode-id>` can be the full id, the queue row triage prints (`#3`), the short id it prints
(`1a2b3c4d:5e6f7a8b`) or any prefix that matches one episode in the queue or in `runs/`. Suggested
commands (`next:` lines, `status`, refusals) and triage rows use the short id whenever it matches that one
episode alone; `--json` output always carries the full id.

Everything is stored in `runs/` in the current folder; set `CULPRIT_RUNS=/path/to/runs` to use another.
Session logs are read from `~/.claude/projects`; set `CULPRIT_PROJECTS=/path/to/projects` to read others.
`status`, `confirm`, `review`, `calibration` and `report` refuse (exit 3) when the store
does not exist rather than create an empty one.

Batch: `uv run culprit diagnose-all` diagnoses every prepared episode.

Trust: label replays with `culprit review` / `culprit label <run> pass|fail`; `culprit calibration`
shows how often the judge agrees with you (calibrated at ≥ 20 labels and Cohen's kappa ≥ 0.6).
`culprit selftest <episode-id>` plants a known fault in each layer of an episode that normally passes
and reports how often culprit names it.

Exit codes: 0 done · 1 refused or failed · 2 bad arguments · 3 not ready (a step that must come first
is missing; the message names it) · 4 spend cap reached · 130 stopped with Ctrl-C (the replay in
flight is killed and its copy deleted; finished replays are kept, so rerunning the same command resumes).

## Cost

Each replay is capped at $2; a full diagnosis is about 30 replays, capped at $40 per episode
(selftest: $40 per planted fault; `diagnose-all`: $400 in total). Judge and drafting calls are capped at
$0.50 each and charged at that cap. Caps are built in and count everything already spent, so resuming
never doubles them; the $400 total covers every batch in `runs/`.
Runs that fail for reasons outside the agent (API errors, culprit's own cap, timeouts) are not counted
and are retried. If no result could beat the baseline at N runs, culprit stops before the oracles and
says "not testable".

Before it spends anything, `baseline`, `diagnose`, `selftest` and `diagnose-all` print on stderr the most
they could run (every variant with all its void retries, plus drafting calls) and spend (that worst case,
limited by what is left of the cap), then start without asking.

## Safety

- Replays run in a copy placed beside your project (`.culprit-replay-<id>`), deleted afterwards.
- No real MCP server ever connects. An episode that used MCP tools gets a stand-in for each server
  that answers only with the results recorded in your log, so Slack, Gmail and similar are never reached.
- Shell commands run in Claude Code's OS-enforced Bash sandbox: they can write only in the copy and the
  temp folder, with no unsandboxed fallback.
- Git remotes are stripped from the copy; `git push` and `gh` are denied; git cannot climb out of the
  copy into a parent repository. Episodes in a git worktree or submodule are refused.
- Paths to your project in the prompt are rewritten to the copy, file tools are denied on your project,
  and if anything in your project still changes during a replay culprit stops and names the files.
- Episodes run in your home folder, in a folder containing culprit, or beside an unwritable parent are
  refused. `runs/` is gitignored and never committed.

## What replays reproduce, and where they fall short

- **Images** you attached are resent with the prompt. An image the log did not keep blocks the replay.
- **Questions Claude asked you** are answered with your recorded answer when the replay asks something
  close (similarity ≥ 0.6); anything else is refused, as a replay with nobody present would be.
- **MCP tools** answer from the log; a call the original session never made fails in the replay.
- **Files** are restored from Claude Code's snapshots and the episode's own edits; for a project in git,
  everything else is rolled back to the last commit before the episode. Outside git, files nothing
  recorded are as they are today.
- **Claude's project memory** is a copy of today's notes, not the notes as they were then.
- **Replays start at the beginning of the episode**, not at the step where it went wrong.
- **A harness verdict** says what made it pass, not whether the fix is yours or Claude Code's. To narrow
  it, replay on another Claude Code build: `CULPRIT_CLAUDE_BIN=/path/to/claude uv run culprit diagnose …`;
  a verdict that flips between builds points at Claude Code.

## Status

Built and unit-tested (199 tests); a final review's critical and important findings are fixed. Triage
and restore have run on real logs; no real episode has been replayed yet — start with one cheap baseline
(`--n 1`) and read its output before anything larger.

Design: [`docs/specs/2026-10-11-culprit-design.md`](docs/specs/2026-10-11-culprit-design.md) ·
Plan: [`docs/plans/2026-10-11-culprit-plan.md`](docs/plans/2026-10-11-culprit-plan.md)
