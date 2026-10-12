# culprit — design

**culprit tells you what to change when a Claude Code episode fails — the task, the environment, the
agent setup, the harness, or the model — by re-running the episode with one layer fixed and seeing
which fix makes it pass.** It proves a cause by intervention, not by asking a judge to guess.

- **Why intervention:** published automatic failure attribution localises the faulty step only
  ~14–24% of the time on hard benchmarks (Who&When, TRAIL), and no shipped tool labels a failure
  model-vs-harness-vs-prompt (survey, 2026-10-10). A re-run that flips fail→pass is evidence; a judge's
  reading of a trace is an opinion.
- **Scope of v1:** the user's own past Claude Code sessions in `~/.claude/projects/*.jsonl`, all four
  failure symptoms, 10–20 episodes, local only.
- **Done means:** for each diagnosed episode, one symptom and one cause from the MECE taxonomy below,
  backed by pass rates; a symptom × cause grid with a ranked fix list; and culprit's own accuracy,
  measured on planted faults.

---

## 1. The taxonomy — two MECE axes

Each axis is an ordered checklist; **the first match wins** (mutually exclusive), and each ends in a
catch-all (collectively exhaustive). The unit is the **episode**: one human prompt and everything
Claude Code did until the next human prompt, identified as `<sessionId>:<promptId>` and cut by file order.

### Symptom — what went wrong (ordered worst first)

| # | Symptom | Test |
|---|---|---|
| 1 | Stopped short | The asked-for outcome was not delivered: gave up, handed work back, asked instead of acting |
| 2 | Wrong result | Delivered, but incorrect: failing tests, wrong file, invented API |
| 3 | Ignored instructions | Correct, but broke a stated rule or ask (CLAUDE.md, a skill, the prompt), including claiming done without required verification |
| 4 | Wasteful | Correct and compliant, but over budget: loops, retries, over-exploration, excess subagents |
| — | No failure | None of the above; the episode is excluded |

### Cause — what to change (ordered cheapest fix first)

| # | Cause | Proven when | You change |
|---|---|---|---|
| 1 | Misjudged | The human (or an independent check) says it was not a failure | Your label |
| 2 | Not reproducible | The unchanged replay already passes most of the time (at least 6 of 10; §3) | Nothing — it was luck |
| 3 | Task | Only the request is restated clearly, and it passes | How you ask |
| 4 | Environment | Only the outside world is fixed (flaky test, network, broken repo state), and it passes | Repo or infrastructure |
| 5 | Agent setup | Same model and tools, better instructions or role, and it passes | CLAUDE.md, rules, skills, subagent definitions |
| 6 | Harness | Same model and instructions, given what it lacked (context, a working tool, permission, an unharmed compaction), and it passes | Tools, MCP, hooks, settings |
| 7 | Model | Only a stronger model or more effort (`fable`, the latest Fable, at `--effort max`) makes it pass | `--effort`, `--model` or routing |
| 8 | Multi-cause | No single variant passes, but a pair does; the pair is reported | Both layers |
| 9 | Unexplained | Nothing passes | Human review |

**Layer boundaries are decided by which knob fixes it:** text the model reads → Agent setup; Claude Code
configuration → Harness; anything outside Claude Code → Environment.

**"Passes" is statistical:** a variant counts only when its pass rate over N replays clearly exceeds the
unchanged baseline's over N replays — the exact test is in §3, *What "passes clearly" means*.

---

## 2. Facts the design rests on (verified 2026-10-11)

**The logs** (structure and counts only; no conversation text read):

| Fact | Value | Used for |
|---|---|---|
| Main session files | 345 (plus 129 subagent logs), 623 MB | Data volume |
| Episodes (`promptId`) | 5,522 | The unit of analysis exists already |
| Interrupts / tool errors / compactions / API errors | 386 / 1,021 / 36 / 25 | Failure-candidate signals (API errors are flagged, not scored) |
| Turn duration p50 / p90 / p99 | 52 s / 241 s / 967 s | Wasteful budget, replay cost |
| `file-history-snapshot` records | 4,853 of 4,933 prompts have one keyed by the prompt record's `uuid` | Restoring the episode's starting state (see below) |
| Tracked file backups | 35,238 entries: 28,370 backup present in `~/.claude/file-history/<sessionId>/`, 6,239 `backupFileName: null`, 629 missing | Overlay onto a copy of the working directory |
| Working directories | 86 distinct: 9 git repos, 56 exist without git, 21 gone | An episode whose `cwd` is gone cannot be replayed |
| Permission modes | `auto` 9,547, `acceptEdits` 3,378, `default` 880 | Replays reuse the episode's mode (see §3) |
| Tool use | Bash 14,940, MCP 4,473, Edit 3,789, AskUserQuestion 220 | MCP calls and answered questions are replayed from the log (§3) |
| Models | ~99% Opus; Fable 303 responses | Fable 5.1 is the oracle model for almost every episode |

**Restoring an episode's starting state** (revised 2026-10-11 after the first real restores). The copy
starts as a clone of the working directory today. Snapshot backups are then put back: Claude Code keys a
snapshot by the episode's prompt `uuid`, and its relative paths are relative to **the directory the
session started in** (97 of 347 sessions later change directory). A snapshot entry with
`backupFileName: null` proves nothing — three files an episode edited carried it — so it is reported as
unknown and left as it is today. Last, every file the episode itself recorded at first touch is set
from that record, which is exact: an `Edit`'s or `Write`'s `originalFile`, or absent for a file a
`Write` created (in the logs: 1,232 recorded originals, 576 creations, 2,559 edits that recorded none).
Before that last step, the restore reports how often the snapshot agreed with those records — the trust
figure for files only the snapshot covers. Files nothing recorded are as they are today; every verdict
says so.

**Claude Code 2.1.296** (`claude --help`, and the headless docs at
<https://code.claude.com/docs/en/headless>, both read 2026-10-11):

- `claude -p` without `--bare` loads the same context as an interactive session (CLAUDE.md, hooks,
  skills, plugins, MCP).
- `--strict-mcp-config` with `--mcp-config` limits MCP servers to the ones passed.
- `--permission-prompts none` removes tools that need a person, such as `AskUserQuestion`.
- Verified live 2026-10-11 on 2.1.296 (the latest release): `AskUserQuestion` is offered headless only when
  `--permission-prompt-tool` names an MCP tool; a `PreToolUse` hook, from `--settings` or the project's
  `.claude/settings.json`, leaves it out of the init tool list. Images reach the model as content blocks
  of a `--input-format stream-json` user message. `sandbox.enabled` with `allowUnsandboxedCommands: false`
  refused a shell write outside the working directory. `autoMemoryDirectory` in `--settings` is read.
  A sandboxed `curl https://example.com` was refused in `acceptEdits` ("user denied") and, in `auto`, as
  "not in this command's allowed_domains" (so a command declaring its domains could be approved); with
  `sandbox.network.strictAllowlist: true` it was refused outright ("host is not on the allow list").
- `--max-budget-usd` caps spend; JSON output reports `total_cost_usd`.
- Measured with two live Haiku calls (about $0.008 in total): `--output-format stream-json` requires
  `--verbose` with `-p`; the last line is `{"type": "result", "subtype": "success", "is_error": false,
  "result": …, "total_cost_usd": …, "num_turns": …}`; `--no-session-persistence` writes no session log
  but still creates an empty `~/.claude/projects/<encoded cwd>/memory/` folder; plugin `SessionStart`
  hooks run.
- Verified live 2026-10-11 on 2.1.296 (Haiku): a copy of a session log cut before a prompt, with every
  `sessionId` set to a fresh uuid4 and every `cwd` pointed at a new folder, written to
  `~/.claude/projects/<encoded new folder>/<new id>.jsonl` (encoding: every non-alphanumeric character of
  the absolute path becomes `-`), is resumed from that folder by `claude -p --resume <new id> --fork-session
  --no-session-persistence`: the model sees the earlier turns and nothing after the cut, and neither the
  original log nor the copy is modified. Without `--system-prompt-snapshot off` the resumed run reuses the
  session's recorded system prompt and ignores `--append-system-prompt`. A CLAUDE.md edited after the
  session started is picked up both by a fresh `-p` run and by a `--resume … --fork-session` run, with or
  without the snapshot flag (live probe, 2.1.296); culprit keeps `--system-prompt-snapshot off` anyway.
- `--json-schema` with `--output-format json` returns schema-checked output in `structured_output`
  (docs); the judge, drafter and leak check use it.

**User-level hooks:** one `SessionStart` hook (`context-mode-cache-heal.mjs`), local only. Project-level
hooks are checked per episode before its first replay.

---

## 3. Architecture

A standalone local Python CLI that replays episodes through headless `claude -p`. Rejected: a Claude
Code plugin (replays would run inside the session being diagnosed and contaminate it) and building on
devi (a different harness from the one being diagnosed). Harbor, a container test runner that can
drive Claude Code, is deferred: it adds Docker and has not been shown to start from a restored repo
state; v1 uses `claude -p` in a throwaway directory and revisits Harbor if isolation proves too weak.

| # | Step (module) | One job | Input → output |
|---|---|---|---|
| 1 | episodes (`logs`) | Split sessions into episodes | `.jsonl` → `Episode{prompt, transcript, model, cwd, branch, snapshot ref, signals}` |
| 2 | triage (`logs`) | Rank failure candidates for the human to confirm, from interrupts, tool errors (capped), compactions and slow turns (an interrupted turn logs no duration, so it is timed from its first to its last timestamp); API errors are infrastructure failures, so they add nothing to the score and a row that has them is marked likely not reproducible; a prompt that is only a slash command (one line: `/`, a command word, its arguments, e.g. `/compact` or `/model opus`) asks Claude Code rather than the agent for something and is never a candidate | Episodes → confirmed episodes, each with a Symptom (human-confirmed; this is the Misjudged check) |
| 3 | criterion (`model`) | Draft what success means, from the human's follow-up messages, the What happened facts and the confirmed symptom (see *The success criterion*) | Episode → success criterion the human approves (`culprit confirm` with no id walks the queue: each row with its What happened recap, which includes the human's next prompt, since whether that prompt corrects Claude is for the human to judge, not a phrase list; symptom, then the drafted criterion to approve, then the restore) |
| 4 | restore (`replay`) | Restore the episode's starting state in a throwaway directory | Episode → directory |
| 5 | oracles (`diagnose`) | Build one variant per cause | Episode → variants: task, environment, agent setup, harness, model |
| 6 | replayer (`replay`, `diagnose`) | Run each variant N times, baseline first | Variant + directory → stream-json transcripts, cost |
| 7 | judge (`model`) | Score a replay against the criterion | Final answer, diff, check results and the transcript's tool calls + criterion → pass/fail; a failing check, a run over the criterion's limits or an errored run fails without a model call |
| 8 | verdict (`diagnose`) | Apply the cause checklist to the pass rates, then the pair test | Pass rates → Cause |
| 9 | report (`report`) | Lead with what to fix first (single-layer fixes ranked by episodes covered), then interacting causes (multi-cause, by layer set), then the grid and trust | Verdicts → report |
| 10 | selftest (`trust`) | Plant known faults and check culprit names them | Planted episodes → accuracy |

The package is standard-library Python in nine modules, one per pipeline stage: `logs` (episodes,
failure signals, triage, locating an episode, snapshots, replay eligibility), `store` (taxonomy and the
plain-file store), `model` (every model call: the locked-down call, criterion drafting, the symptom
suggestion, leak check, judge), `replay` (restore and one replay), `stubs` (the stand-in MCP servers, run as their own light
processes: `python -m culprit.stubs mcp …` and `python -m culprit.stubs answers …`), `diagnose`
(variants, oracles, verdict and its statistics, cost preview, the whole-queue batch), `trust` (judge
calibration and selftest), `report` (the report and per-episode status) and `cli`.

Storage is plain files: one folder per episode holding its variants, transcripts and verdict JSON, under
`runs/` (or `$CULPRIT_RUNS`); the session logs are read from `~/.claude/projects` (or
`$CULPRIT_PROJECTS`). Planted-fault runs live under `runs/selftest/`, apart from real episodes.

Models are named by Claude Code alias (2.1.296: `fable`, `opus`, `sonnet`, `haiku`, each the latest model of
its tier), so culprit follows new releases without a code change: the model oracle uses `fable`, every
drafting, suggestion, leak-check and judge call `sonnet`, and selftest's weak-model fault `haiku`. `CULPRIT_ORACLE_MODEL`,
`CULPRIT_DRAFTING_MODEL` and `CULPRIT_WEAK_MODEL` pin another alias or id; each is read when it is used.

culprit knows four families (`fable`, `opus`, `sonnet`, `haiku`, parsed from ids like `claude-<family>-<version>`).
A model of any other family may be a newer, stronger tier the oracle alias does not cover, so when the logs show
one, `triage` (once per run, listing every such id among the episodes it scanned) and single-episode `diagnose`
print a one-line warning on stderr: "your sessions used <id>; if it is stronger than <resolved oracle>, set
CULPRIT_ORACLE_MODEL=<id>". `triage` calls no model, so it names the oracle as last resolved (or by its alias);
`diagnose` resolves it first. No warning is given for the model `CULPRIT_ORACLE_MODEL` already names. Only the
unknown-family case is warned about: whether a known family's newer release outranks Fable is not knowable from
the logs.

### The command line

Eight commands: `triage`, `confirm`, `env-fix`, `diagnose`, `report`, `status`, `review` and `selftest`.
The only flags are `triage --top` and `status --json`; everything else comes from the two environment
variables above. An episode is named by its row number in the triage queue (plain `3`; a shell drops
`#3` as a comment), its short id or a unique prefix. `confirm` with no id walks the queue in a terminal;
`confirm <episode> <symptom>` records the symptom, drafts the criterion and, in a terminal, asks to approve
it (approve, edit first, skip); `confirm <episode>` shows the draft and asks again once a symptom is recorded;
`confirm <episode> approve` is the only way to approve without a terminal, and confirming a symptom again never
approves;
`confirm <episode>` (with or without a symptom), each episode of the walk and `status <episode>` first show
What happened, built from the episode's records with no model call: what the person asked (about 300
characters), what Claude did (tool calls; files Edit, Write and NotebookEdit targeted, relative to the working
directory, at most five then "+N more"; tool errors and the first one's first line), Claude's last text, how it
ended (the person interrupted it, it finished — each with how long it took — or API errors ended it) and the
person's next prompt; a missing session log leaves it out.
Below it comes a suggested symptom, `Suggested: wrong-result — <reason>`: one `sonnet` call shown only those
facts (never the criterion) picks one of the five symptoms and gives a non-empty one-line reason citing a fact
from the block; the call is charged to the episode at its cap and the answer kept in
`runs/<episode>/suggestion.json`, so it is paid for once. It is asked only when the symptom question is about to
be asked (no recorded symptom and none typed); otherwise a stored suggestion is shown beside the recorded or
typed symptom, without a warning when they differ. In a terminal the question lists the five symptoms, each
with its one-line meaning, and Enter accepts the suggestion; a failed call leaves no suggestion and no
default. Without a terminal, `confirm <episode>` with no symptom recorded shows What happened, the suggestion
and the five meanings, names `culprit confirm <episode> <suggested>` as the next step and records nothing. No
symptom is ever recorded without the person choosing it (Enter on a shown suggestion counts);
`diagnose` restores the starting files when they are missing, runs the baseline and the oracles, and with
no id diagnoses the whole queue and writes the report. Every replaying command prints the most it can run
and spend before it starts.

### The success criterion

The drafter (`sonnet`, one call) is shown the request, the person's later messages, the What happened facts,
what the original attempt took (tool calls, minutes, and cost when the log has it) and the confirmed symptom
with its meaning. A criterion that only describes the outcome cannot fail an attempt for the way it failed when
the symptom is not about the outcome, so the symptom shapes the draft:

| Symptom | What the draft must carry |
|---|---|
| stopped-short | `success_means` names every part of the request that must be finished |
| wrong-result | the correct outcome |
| ignored-instructions | each ignored instruction as its own explicit line: must in `success_means`, must not in `must_not` |
| wasteful | the outcome plus **limits**: `max_tool_calls` and `max_minutes` (and `max_usd` only when the original's cost is known), clearly tighter than the original — about half — with a one-line `limits_reason`; the call's schema requires them, and a draft without them falls back to half of what the original took |
| no-failure | the outcome the attempt reached |

Other symptoms may set limits but usually do not. The leak rule is unchanged: describe the outcome, never the
fix, the file or the code. `criterion.json` keeps `limits` (`{max_tool_calls, max_minutes, max_usd}`, any of
them null), `limits_reason`, the `symptom` it was drafted for and `drafted_hash`, a hash of what the draft said
when written; a file written before these fields loads with no limits and no symptom.

**Limits are checked from the run, never by the model.** Before the judge's model call, a run fails with
`by: limit` and a reason such as `over the limit: 31 tool calls > 15` when it made more tool calls than
`max_tool_calls` (counted from the transcript: the agent's own calls, not a subagent's, which carry a
`parent_tool_use_id`, since the original's log holds only the agent's own), ran longer than `max_minutes` (the
result line's `duration_ms`, else the wall clock; a run killed at culprit's time limit took all of it) or cost
more than `max_usd` (the result's `total_cost_usd`; a run killed at the time limit has no known cost, so its
cost is not held against it). Within its limits a run is judged as before. The criterion block that `confirm`,
`status` and the report print shows them under **Limits**: `at most 15 tool calls · 4 minutes — <reason>`.
**A changed symptom redrafts, but never over a person's edits or an approval.** On `confirm <episode>
<symptom>` (the symptom typed), a draft that is not approved and was written for another symptom, or carries no
symptom tag (drafted before drafts were tagged), is redrafted for the typed symptom — one call, printed as
`✓ Redrafted for <symptom>` — when what it says still hashes to its `drafted_hash`. A draft without a hash
cannot show an edit, so it counts as unedited; since the tag and the hash were added together, that is exactly
the untagged legacy draft, and a person's edits to one before this change are lost to the redraft. An edited
draft is kept, with `Kept your edited draft; it was written for <old> — edit runs/…/criterion.json to match
<symptom>`. An approved criterion is never redrafted; when its tag names another symptom, `confirm` says so.
`confirm <episode>` with no symptom and the queue walk never redraft.

Limits are part of the criterion's key, so changing them retires earlier runs like any other change; a
criterion without limits keys as it did before limits existed.

### Every replay runs locked down

```
claude -p \                      # the episode or variant prompt goes on stdin
  --output-format stream-json --verbose \
  --strict-mcp-config --mcp-config '<stand-ins only, or {"mcpServers":{}}>' \
  --permission-prompts none --permission-mode <mode> \
    # with recorded answers: --permission-prompts host --permission-prompt-tool <answer host>
  --no-session-persistence --max-budget-usd <cap> \
  --settings '<Bash sandbox (strict empty network allow list), autoMemoryDirectory, culprit hooks>' \
  --model <model> \
    # a follow-up episode: --resume <fork id> --fork-session --system-prompt-snapshot off
  --disallowedTools "Bash(git push *)" "Bash(gh *)" <file tools on the original folder>
```

- **A follow-up starts from its session's earlier conversation.** For an episode that is not its
  session's first prompt, culprit copies every record before the episode's prompt (by file order), gives
  the copy a fresh session id, points every path into the original project (each `cwd` and any path the
  conversation mentions) at the clone, writes it under the clone's folder in `~/.claude/projects/` and
  resumes it as a fork with the system-prompt snapshot off, so the fork reads the clone's CLAUDE.md as
  it is now, the agent-setup oracle's addition included. Every variant gets the same history; the task oracle restates only the episode's own prompt.
  A session's first prompt replays with no history, as before. The original log is only read.
- **No real MCP server ever connects.** For each server an episode used, culprit runs a stand-in that
  answers recorded calls with their recorded results and fails any call the original never made.
- **Network shell commands are answered from the log.** The sandbox reaches no host. culprit adds its
  own `PreToolUse` hook for `Bash` through `--settings` (`python -m culprit.stubs bash-hook
  <recordings>`), beside the user's own hooks, which still run as in the original. From the episode's
  records culprit lists every `Bash` call whose command runs a network tool (curl, wget, gcloud, gsutil,
  bq, psql, mysql, aws, az, kubectl, ssh, scp, sftp, rsync to a remote or over `-e ssh`, httpie's
  `http`/`https`, redis-cli, mongosh, mongo, sqlcmd, firebase, vercel, supabase, heroku, stripe, nc,
  telnet, ftp, doctl, flyctl, netlify, wrangler, helm, terraform — at any command position, behind
  `sudo`/`env`/`timeout`/assignments/`bash -c`), with its recorded output and exit status ("Exit code N"
  taken off the front of a failed result; a failure with none exits 1). An incoming command that,
  normalised for whitespace, exactly matches a recorded one (its original-project paths pointed at the
  clone) is allowed with `updatedInput` replacing it by `cat <recorded output>; exit <status>`; a
  command recorded several times gets each answer in turn, then the last again. Anything else gets no
  reply, so the sandbox refuses a live call. `gh` and `git push` are not on the list; they stay denied.
  Every network call the hook sees is logged in the run folder (`network-log.jsonl`), served or not;
  each run's result counts them (`served`, `refused`), each variant adds up its counted runs, and
  `status`, `diagnose` and the report show the counts per variant ("network calls: baseline 12 served
  from the log, 3 refused as unrecorded") beside the runs stopped at a limit, so a verdict built on
  refused calls is visible.
  Verified live 2026-10-11 on 2.1.296 (Haiku): a `--settings` `PreToolUse` hook's `updatedInput`
  replaced a Bash command, its output and exit status reached the model, and the transcript kept the
  original command; a user hook that blocks the command still wins.
- **WebFetch and WebSearch are never live.** They run outside the shell sandbox (its allowed domains do
  not limit WebFetch), so culprit adds a second `PreToolUse` hook, matcher `WebFetch|WebSearch`
  (`python -m culprit.stubs web-hook <recordings>`), that denies every call. A call identical to one the
  episode made (WebFetch: same `url` and `prompt`; WebSearch: same `query`) is denied with the reason
  "offline replay; the recorded result of this exact call was: …" carrying the recorded result (cut at
  20,000 characters); any other is denied with "offline replay: not recorded". If the hook itself fails
  it still denies, and if its process cannot start the hook command exits 2, which blocks the call. Both
  hooks run `python -P`, so files in the clone (a project's `json.py`) cannot shadow the standard library. This is an approximation: the agent sees a denial holding the content, not a normal
  tool result, and may treat it differently. A command built without culprit's hooks drops both tools
  through `--disallowedTools`. Each call is logged with the shell calls in `network-log.jsonl`.
- **Questions are answered from the log:** culprit's answer host is the permission-prompt tool; a
  question close to one the human answered (difflib ratio ≥ 0.6) gets that answer, and every other
  question or permission prompt is refused.
- **Shell commands run in the OS Bash sandbox** (writes only in the copy and the temp folder, no
  unsandboxed fallback, a strict and empty network allow list so no shell command reaches any host); file tools are denied on the original folder; a before/after fingerprint of the
  original stops culprit if anything changed. Images are resent with the prompt.
- **The working directory is an APFS clone** (`cp -cR`) of a pristine restored copy, one fresh clone
  per replay, with every git remote removed; `git push` and `gh` are also denied.
- **Each clone sits beside the original working directory** (`<parent>/.culprit-replay-<id>`), so the
  replay loads the same ancestor `CLAUDE.md` files the original session did. The original project's
  auto-memory is copied per run and read through `autoMemoryDirectory` (today's notes, not the notes as
  they were); the replay's writes never reach the real notes.
- **Permission mode:** the episode's own mode (`auto`, `acceptEdits`), except `default` becomes
  `acceptEdits`, because in the original the human approved those prompts and no human is present now.
- **Cleanup:** after each replay culprit deletes the clone, the forked session copy, and the clone's
  folder under `~/.claude/projects/` when nothing but an empty `memory/` folder is left in it.
- **Spend and time:** each replay's caps follow the original episode. `--max-budget-usd` is 3× what the
  episode cost, at least $10 (the harness variant doubles it); the time limit is 3× how long it took, at
  least 1,800 s. The cost is the session's running total (`cost-state` records' `totalCostUSD`) at the
  episode's last such record less the last one before it (nothing before a session's first episode); with no
  record inside the episode, or none before a follow-up, it is unknown and the floor applies — measured
  2026-10-11, 79 of 5,256 real episodes (1.5%) had one and 35 (0.7%) could be costed. The duration is the failure signals' (the sum of its
  `turn_duration` records, else its first-to-last timestamp span; 90% have `turn_duration`). Each model call is
  capped at $0.50. Built-in caps stop further replays: per episode and per planted fault, the worst case at
  N = 10 for that episode's per-replay cap (rounded up to $50: $3,900 and $3,000 at the $10 floor), and
  for a whole-queue `diagnose` 20 episodes at the floor's worst case ($78,000), inside which each episode
  keeps its own cap (see §5). The plan printed before spending shows the per-replay cap.

### Which episodes are replayable

An episode is replayable only if its `cwd` still exists and is not the home folder, a folder containing
culprit, a git worktree or submodule, or beside an unwritable parent; its snapshot exists; any question it
asked has a recorded answer; any image it carried was kept in the log; and, for a follow-up, its prompt
record has the `uuid` the session copy is cut at. `triage` lists blocked candidates after every replayable one and prints the blocking reason under each,
and the restore (run by the `confirm` walk or by `diagnose`) refuses with the reasons; it also lists every
hook the project's own settings define, because those hooks run in every replay.

### The oracles, concretely

| Variant | What changes from the baseline | Untested when |
|---|---|---|
| Task | The prompt, rewritten from the original prompt and the approved criterion to state scope and success criterion (the follow-ups reach it only through the criterion, which limits leaks; the leak check still compares the draft with them); for a follow-up, the drafter also sees the session's earlier conversation | The leak check rejects it twice |
| Environment | A fix script the human supplies (`culprit env-fix`) runs in the clone before the replay | No script supplied (the common case in v1) |
| Agent setup | Standing instructions drafted from the criterion, the instructions the agent already loads and the starting folder layout (gaps filled, no task-specific facts, at most 12 lines), appended to the clone's project `CLAUDE.md` under a `## More working rules` heading before Claude starts (`.claude/CLAUDE.md` when only that exists; a new `CLAUDE.md` when neither does) — the way the real fix would arrive, as a user message, not through the stronger `--append-system-prompt` channel. The addition is removed again before the diff, so the judge sees only the agent's changes; combined and pair variants write it too | The leak check rejects it twice |
| Harness | The default tool set with every non-safety restriction lifted, the files the episode read or edited preloaded into the prompt as context, and double the spend cap | Never |
| Model | `--model fable --effort max` (an episode already on what `fable` means today keeps its model and only raises the effort); see the effort step below | The episode already ran on what `fable` resolves to today (an older Fable is still tested), at max effort |

**The model oracle's cheaper-fix steps.** Every replay runs at the effort level the episode logged (the `effort`
field on its first assistant record; none logged leaves Claude Code's default), and the model oracle may change
only `model` and `effort`. `max` is the highest `claude --effort` level (2.1.296: low, medium, high, xhigh,
max). When Fable at max effort beats the baseline, up to two more variants run, everything else as the baseline:

- `original-max-effort` — the episode's own model at max effort (would raising the effort do?). Skipped when the
  episode ran on the oracle's model (the oracle already was its model at max effort: "raise effort (Fable at max
  effort passed …)") or already ran at max effort (its baseline is that replay).
- `latest-in-family` — the alias of the episode's own family (`opus`, `sonnet`, `haiku`, parsed from ids like
  `claude-<family>-<version>`) at the episode's original effort (would upgrading within the family do?). Skipped
  when the family is unknown, when the family alias resolves to the episode's own model (it is the latest
  already), or when the family is the oracle's (then it is Fable minus the effort, which the steps above cover).

The verdict stays Model — the taxonomy and cause order are fixed — and its evidence names every cheaper fix that
beat the baseline, cheapest first (effort, then the same-family upgrade): "raise effort (original model at max
effort passed 7/10)", "upgrade to the latest opus (claude-opus-5-5 passed 8/10)" (the id it ran as), both joined
by ", or ", else "needs Fable (…)" with what did not pass, e.g. "needs Fable (original model at max effort did not
pass: 1/10; the latest opus, claude-opus-5-5, did not pass: 2/10)". The cost preview and the episode and fault
caps count each step whenever it could run.

**What the oracle alias means today.** Whether an episode already ran on the strongest model is decided against
the id the oracle alias resolves to, not a name prefix. culprit resolves it for free: `claude -p --model <alias>
--output-format stream-json --verbose --tools "" --no-session-persistence --max-budget-usd 0.05` with the local
command `/cost` on stdin answers without calling a model, yet its `init` event names the resolved model
(verified 2026-10-11 on 2.1.296: `fable` → `claude-fable-5-1`, `opus` → `claude-opus-5-5`, $0, no turns). If a
build stops naming it, one trivial prompt under the same $0.05 cap reads the same event, and that call's cost
(its cap when unknown) is charged to the episode being diagnosed. The answer is kept in `runs/models.json` with
its time and refreshed after a day; the cost preview reads only that file, and with no fresh answer it counts the
model oracle as runnable. An alias that cannot be resolved never skips the model oracle.

**Which model ran.** A replay asked for an alias runs as whatever that alias means on the day, so each replay's
`result.json` records the model id it actually ran as: the `model` of its stream-json `init` event, else the first
assistant message naming a real model. Each variant's outcome counts its counted runs by that id (`models`), and
`diagnose`, the report and `status --json` show it, e.g. "model test: Fable at max effort, ran as
claude-fable-5-1". Model calls through `ask_claude` keep the model their JSON result's `modelUsage` names (the
one that wrote most) on the stored judgement, criterion and oracle draft.

**What a drafter may see.** Anything the agent could have known before the episode started; nothing from
during or after it (the leak check against the prompt and follow-ups is unchanged).

- *Task*, for a follow-up episode, also sees the session's earlier conversation: the human prompts and the
  agent's text replies before the episode's prompt (no tool calls or outputs), oldest first, at most 20,000
  characters kept from the end. The replay keeps that conversation, so the drafter is told it is context
  the agent already has and the rewrite still restates only the request. A session's first prompt gets
  no history.
- *Agent setup* sees every instruction Claude Code loads when the replay launches, mirroring its documented
  launch-time load order (code.claude.com/docs/en/memory): managed policy (`/Library/Application
  Support/ClaudeCode/CLAUDE.md`, `/etc/claude-code/CLAUDE.md`, and a `claudeMd` string in
  `managed-settings.json`); the user's `~/.claude/CLAUDE.md` and `~/.claude/rules/**/*.md` (replays run with
  the user's config); then `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` in every folder from `/`
  down to the working folder, then the project's `.claude/rules/**/*.md`. `AGENTS.md` is read in their place
  only when none of those three exist at any level. `@path` imports are followed (5 deep, no cycles). Left
  out, as Claude Code leaves them out at launch: `CLAUDE.md` files in subfolders and rules with a `paths:`
  frontmatter (both load on demand), files over 4 MiB, and files a `claudeMdExcludes` glob in the user or
  project settings matches (never managed policy). Files under the working folder are read from the
  restored starting copy, so edits made during or after the episode never reach the drafter; folders above
  it are read from disk as they are now, as the replay itself reads them. Together at most 200,000
  characters, keeping the files nearest the working folder when trimming. It also sees the starting copy's
  top two folder levels (skipping `.git`, `node_modules`, virtualenvs, caches and build output), at most
  150 lines. The draft is appended to those instructions, so it is told to fill their gaps rather than repeat
  them. Alongside the text it returns a suggested placement — project `CLAUDE.md`, `CLAUDE.local.md`,
  `.claude/rules/<topic>.md` with `paths:`, `~/.claude/CLAUDE.md` or managed policy — and a one-line
  reason. The variant file stores that suggestion and the file the test wrote; the report shows under an
  Agent setup verdict "adding these lines to <project>/CLAUDE.md made it pass", the lines, and the
  placement labelled as a suggestion that was not tested (only the project `CLAUDE.md` placement is).

### What "passes clearly" means

Each variant and the baseline run **N = 10** times (raised from 5 so smaller improvements show: 4/10
against 0/10 is significant, where at N = 5 it took 4/5 against 0/5). A variant beats the baseline when a
one-sided Fisher exact test on their pass counts gives p < 0.05.
**Not reproducible** means the baseline itself passed more than half its runs (at least 6 of 10). The pair test first runs every tested variant combined: if
that fails, the cause is **Unexplained**; if it passes, pairs are tried in cause order and the first pair
that beats the baseline is the **Multi-cause** verdict; if no pair does, the verdict is Multi-cause with
the full set.

**Runs that hit a limit are counted.** A replay stopped by its spend cap (`error_max_budget_usd`) or by
culprit's time limit is a counted run, judged like any other on what it produced — its diff, its checks and the
tool calls in its transcript (a timed-out run keeps whatever it printed before it was killed), with the judge
told it was cut off; it usually fails. The run records `limit: budget` or `limit: time`, and `status`,
`diagnose` and the report count the runs each variant lost to a limit. Only infrastructure failures are
**void** (not counted, retried up to 5 times per variant): an API error or overload (`api_error_status`), the
CLI failing during execution (`error_during_execution`), crashing or never starting (no result line), and
culprit's own errors. This follows Inspect AI's practice — sample limits "don't result in errors, but rather an
early exit… still scored". Counting cap hits as void made "ran out of room" invisible in the baseline, so the
harness oracle's doubled budget could never show its effect.

Every verdict states the numbers behind it — the baseline's pass count, the winning variant's (the pair or
`combined` for Multi-cause) and the Fisher p, e.g. `harness: baseline 0/10 → 8/10, p = 0.0004` — in
`diagnose` output and under each verdict in the report. Verdicts with no winner show the baseline only.

**Cost per variant.** Each run keeps its token counts — input, output, cache writes and cache reads — from the
stream-json result line's `usage` (a run with no result line, such as one stopped at the time limit, adds up
the `usage` its assistant messages reported, each message once), beside its `total_cost_usd`. Each variant's
outcome adds them up over every attempt, void ones included, as its cost does. The numbers line ends with the
winning variant's (else the baseline's) cost per run, e.g. `harness: baseline 0/10 → 8/10, p = 0.0004 ·
$0.41/run, 52k tokens/run` (all four kinds of token together), and a `cost per run:` line lists every variant
with its tokens split into input, output and cached; both appear in `diagnose` output and under each verdict
in the report, and `status --json` gives each outcome's totals plus `usd_per_run` and `tokens_per_run`.

---

## 4. Validity guards

1. **Oracle leak check.** An oracle may add only information its actor could legitimately have had.
   A restated task or improved instruction that contains answer-specific details from the human's
   follow-ups is rejected before it runs; otherwise verdicts drift toward Task and Agent setup.
2. **Judge calibration.** The judge's pass/fail is compared with the human's labels on ~20 replays
   before any verdict counts; its agreement is reported with every result.
3. **Baseline before variants.** Every episode is replayed unchanged N times first; that baseline is
   what makes "Not reproducible" detectable.
4. **Planted-fault accuracy.** selftest takes a control episode (one whose baseline passes at least 4 in 5
   of the N runs, 8 of 10), breaks one layer on purpose, and checks culprit names it: a truncated prompt (expect Task), a
   misleading instruction added to the project CLAUDE.md (expect Agent setup), the edit tools removed (expect Harness), a weak
   model (expect Model), and an impossible success criterion (expect Unexplained, the outcome a broken
   check produces — catching that case is what judge calibration is for). The share named correctly is
   printed beside every verdict.
5. **Judge calibration threshold.** Verdicts are marked *uncalibrated* until the human has labelled at
   least 20 replays and the judge's agreement with them reaches Cohen's kappa ≥ 0.6.

## 5. Known limits, reported rather than hidden

- An episode in which Claude asked the human a question replays only when the log recorded the
  human's answer (the answer host gives it back); one with no recorded answer is not replayable.
- Episodes already run on what `fable` resolves to today, at max effort, cannot test the Model cause; reported as
  untestable. An episode on an older Fable (say claude-fable-5 once claude-fable-5-1 exists) is tested.
- The Agent setup addition is an uncommitted change to the clone's `CLAUDE.md`, so an agent that runs
  `git status` or `git diff` can see it, where a real fix would be committed.
- An MCP call or a question the original session never made cannot be answered in a replay. Nor can a
  network shell command that is not identical to one the episode ran: the sandbox refuses it, and a
  command whose network tool is not on culprit's list is simply refused by the sandbox, unrecorded.
  A recorded WebFetch or WebSearch result reaches the replay only as the reason for a denial, an
  approximation of the original tool result; any other web call is denied.
- For a project in git, files nothing else recorded come from the last commit before the episode;
  elsewhere they are as they are today.
- v1 replays from the episode's start (with its session's earlier conversation in place), not from the
  step where it went wrong; mid-episode branching is deferred until v1 proves the method.
- A late-session episode re-sends its whole earlier conversation on every replay, so it costs more per run
  and can hit the per-replay cap; a run stopped by the cap counts, and usually fails.
- A Harness verdict cannot say whether the fix is yours (configuration) or the vendor's (a Claude Code
  bug): an intervention shows what made it pass, not who must change it. The human reads the evidence.
- Cost: about 60 replays per episode (baseline ×10 plus five variants ×10), ~50 minutes at the median turn
  length. Built-in caps are a safety net sized from the worst case at N = 10 (every oracle, combined and
  every pair, both model steps, all void retries) at the episode's per-replay cap: $3,900 per episode and $3,000 per
  planted fault at the $10 floor, $78,000 per queue. v1 is capped at 10–20 episodes.
- The logs are client work; everything runs locally and only the model calls Claude Code already makes
  leave the machine.

## 6. Build order

1. episodes + triage on the real logs, with human confirmation (no replays yet).
2. criterion + restore; prove one episode's starting state restores.
3. replayer + baseline for one episode.
4. oracles + verdict for one episode, end to end.
5. judge calibration and selftest.
6. report across 10–20 episodes.
