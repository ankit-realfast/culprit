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
Claude Code did until the next human prompt, identified by the log's `promptId`.

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
| 2 | Not reproducible | The unchanged replay already passes at a rate indistinguishable from the best variant | Nothing — it was luck |
| 3 | Task | Only the request is restated clearly, and it passes | How you ask |
| 4 | Environment | Only the outside world is fixed (flaky test, network, broken repo state), and it passes | Repo or infrastructure |
| 5 | Agent setup | Same model and tools, better instructions or role, and it passes | CLAUDE.md, rules, skills, subagent definitions |
| 6 | Harness | Same model and instructions, given what it lacked (context, a working tool, permission, an unharmed compaction), and it passes. Tagged **yours** or **vendor's** | Tools, MCP, hooks, settings |
| 7 | Model | Only a stronger model (`claude-fable-5-1`) makes it pass | `--model` or routing |
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
| Interrupts / tool errors / compactions / API errors | 386 / 1,021 / 36 / 25 | Failure-candidate signals |
| Turn duration p50 / p90 / p99 | 52 s / 241 s / 967 s | Wasteful budget, replay cost |
| `file-history-snapshot` records | 4,853 of 4,933 prompts have one keyed by the prompt record's `uuid` | Restoring the episode's starting state (see below) |
| Tracked file backups | 35,238 entries: 28,370 backup present in `~/.claude/file-history/<sessionId>/`, 6,239 `backupFileName: null`, 629 missing | Overlay onto a copy of the working directory |
| Working directories | 86 distinct: 9 git repos, 56 exist without git, 21 gone | An episode whose `cwd` is gone cannot be replayed |
| Permission modes | `auto` 9,547, `acceptEdits` 3,378, `default` 880 | Replays reuse the episode's mode (see §3) |
| Tool use | Bash 14,940, MCP 4,473, Edit 3,789, AskUserQuestion 220 | Episodes that used MCP or asked a question are not replayable in v1 |
| Models | ~99% Opus; Fable 303 responses | Fable 5.1 is the oracle model for almost every episode |

**Restoring an episode's starting state.** Claude Code backs up every file it edits. The snapshot keyed
by the episode's prompt `uuid` maps each tracked path (relative to `cwd`, or absolute) to a backup file;
`backupFileName: null` is read as "the file did not exist yet" — **an assumption Stage 2 verifies on a
real episode** by comparing a restored file with what the episode's first `Read` of it returned. Files
Claude Code never edited are not backed up, so they are taken from the working directory as it is today:
restore fidelity is exact for tracked files and best-effort for the rest, and every verdict says so.

**Claude Code 2.1.296** (`claude --help`, and the headless docs at
<https://code.claude.com/docs/en/headless>, both read 2026-10-11):

- `claude -p` without `--bare` loads the same context as an interactive session (CLAUDE.md, hooks,
  skills, plugins, MCP).
- `--strict-mcp-config` with `--mcp-config` limits MCP servers to the ones passed.
- `--permission-prompts none` removes tools that need a person, such as `AskUserQuestion`.
- `--max-budget-usd` caps spend; JSON output reports `total_cost_usd`.
- Measured with two live Haiku calls (about $0.008 in total): `--output-format stream-json` requires
  `--verbose` with `-p`; the last line is `{"type": "result", "subtype": "success", "is_error": false,
  "result": …, "total_cost_usd": …, "num_turns": …}`; `--no-session-persistence` writes no session log
  but still creates an empty `~/.claude/projects/<encoded cwd>/memory/` folder; plugin `SessionStart`
  hooks run.
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

| # | Unit | One job | Input → output |
|---|---|---|---|
| 1 | episodes | Split sessions into episodes | `.jsonl` → `Episode{prompt, transcript, model, cwd, branch, snapshot ref, signals}` |
| 2 | triage | Rank failure candidates for the human to confirm | Episodes → confirmed episodes, each with a Symptom (human-confirmed; this is the Misjudged check) |
| 3 | criterion | Draft what success means, from the human's follow-up messages | Episode → success criterion the human approves |
| 4 | sandbox | Restore the episode's starting state in a throwaway directory | Episode → directory |
| 5 | oracles | Build one variant per cause | Episode → variants: task, environment, agent setup, harness, model |
| 6 | replayer | Run each variant N times, baseline first | Variant + directory → stream-json transcripts, cost |
| 7 | judge | Score a replay against the criterion | Transcript + criterion → pass/fail |
| 8 | verdict | Apply the cause checklist to the pass rates, then the pair test | Pass rates → Cause |
| 9 | report | Roll verdicts into the grid and a ranked fix list | Verdicts → report |
| 10 | selftest | Plant known faults and check culprit names them | Planted episodes → accuracy |

Storage is plain files: one folder per episode holding its variants, transcripts and verdict JSON.

### Every replay runs locked down

```
claude -p "<episode or variant prompt>" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --permission-prompts none --permission-mode <mode> \
  --disallowedTools "Bash(git push *)" "Bash(gh *)" \
  --no-session-persistence --max-budget-usd <cap> \
  --output-format stream-json --verbose \
  --model <model>
```

- **No MCP server connects in v1**, so no outbound server (Slack, Gmail, Salesforce, monday) can be
  reached. An episode whose transcript used an MCP tool is therefore not replayable in v1.
- **The working directory is an APFS clone** (`cp -cR`) of a pristine restored copy, one fresh clone
  per replay, with every git remote removed; `git push` and `gh` are also denied.
- **Each clone sits beside the original working directory** (`<parent>/.culprit-replay-<id>`), so the
  replay loads the same ancestor `CLAUDE.md` files the original session did. The original project's
  auto-memory is not loaded, because the replay's working directory is new; every report says so.
- **Permission mode:** the episode's own mode (`auto`, `acceptEdits`), except `default` becomes
  `acceptEdits`, because in the original the human approved those prompts and no human is present now.
- **Cleanup:** after each replay culprit deletes the clone and the empty `memory/` folder Claude Code
  leaves under `~/.claude/projects/`.
- **Spend:** `--max-budget-usd` per replay, and a per-episode cap that stops further replays.

### Which episodes are replayable

An episode is replayable only if its `cwd` still exists, its snapshot exists, it used no MCP tool, and it
never called `AskUserQuestion`. culprit reports the reason for every episode it skips.

### The oracles, concretely

| Variant | What changes from the baseline | Untested when |
|---|---|---|
| Task | The prompt, rewritten from the original prompt and the human's follow-ups to state scope and success criterion | The leak check rejects it twice |
| Environment | A fix script the human supplies (`culprit env-fix`) runs in the clone before the replay | No script supplied (the common case in v1) |
| Agent setup | An appended system prompt (`--append-system-prompt`) drafted from the criterion: standing instructions, no task-specific facts | The leak check rejects it twice |
| Harness | The default tool set with every non-safety restriction lifted, the files the episode read or edited preloaded into the prompt as context, and double the spend cap | Never |
| Model | `--model claude-fable-5-1` | The episode already ran on Fable |

### What "passes clearly" means

Each variant and the baseline run **N = 5** times. A variant beats the baseline when a one-sided Fisher
exact test on their pass counts gives p < 0.05 (for example 4/5 against 0/5). **Not reproducible** means
the baseline itself passed at least 3 of 5. The pair test first runs every tested variant combined: if
that fails, the cause is **Unexplained**; if it passes, pairs are tried in cause order and the first pair
that beats the baseline is the **Multi-cause** verdict; if no pair does, the verdict is Multi-cause with
the full set.

---

## 4. Validity guards

1. **Oracle leak check.** An oracle may add only information its actor could legitimately have had.
   A restated task or improved instruction that contains answer-specific details from the human's
   follow-ups is rejected before it runs; otherwise verdicts drift toward Task and Agent setup.
2. **Judge calibration.** The judge's pass/fail is compared with the human's labels on ~20 replays
   before any verdict counts; its agreement is reported with every result.
3. **Baseline before variants.** Every episode is replayed unchanged N times first; that baseline is
   what makes "Not reproducible" detectable.
4. **Planted-fault accuracy.** selftest takes a control episode (one whose baseline passes at least 4 of
   5), breaks one layer on purpose, and checks culprit names it: a truncated prompt (expect Task), a
   misleading appended instruction (expect Agent setup), the edit tools removed (expect Harness), a weak
   model (expect Model), and an impossible success criterion (expect Unexplained, the outcome a broken
   check produces — catching that case is what judge calibration is for). The share named correctly is
   printed beside every verdict.
5. **Judge calibration threshold.** Verdicts are marked *uncalibrated* until the human has labelled at
   least 20 replays and the judge's agreement with them reaches Cohen's kappa ≥ 0.6.

## 5. Known limits, reported rather than hidden

- Episodes in which Claude asked the human a question cannot be replayed faithfully (no person to
  answer); they are tagged and excluded from v1.
- Episodes already run on Fable cannot test the Model cause; reported as untestable.
- Episodes that used an MCP tool are excluded from v1, because replays connect no MCP server.
- v1 replays from the episode's start, not from the step where it went wrong; mid-episode branching is
  deferred until v1 proves the method.
- Cost: about 30 replays per episode (baseline ×5 plus five variants ×5), ~25 minutes at the median turn
  length. v1 is capped at 10–20 episodes.
- The logs are client work; everything runs locally and only the model calls Claude Code already makes
  leave the machine.

## 6. Build order

1. episodes + triage on the real logs, with human confirmation (no replays yet).
2. criterion + sandbox; prove one episode's starting state restores.
3. replayer + baseline for one episode.
4. oracles + verdict for one episode, end to end.
5. judge calibration and selftest.
6. report across 10–20 episodes.
