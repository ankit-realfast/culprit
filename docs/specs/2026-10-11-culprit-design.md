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
unchanged baseline's over N replays (the threshold is fixed in the implementation plan, not here).

---

## 2. Facts the design rests on (verified 2026-10-11)

**The logs** (structure and counts only; no conversation text read):

| Fact | Value | Used for |
|---|---|---|
| Main session files | 345 (plus 129 subagent logs), 623 MB | Data volume |
| Episodes (`promptId`) | 5,522 | The unit of analysis exists already |
| Interrupts / tool errors / compactions / API errors | 386 / 1,021 / 36 / 25 | Failure-candidate signals |
| Turn duration p50 / p90 / p99 | 52 s / 241 s / 967 s | Wasteful budget, replay cost |
| `file-history-snapshot` records | 6,093 (plus `cwd`, `gitBranch`) | Restoring the episode's starting state — **feasibility unverified** |
| Models | ~99% Opus; Fable 303 responses | Fable 5.1 is the oracle model for almost every episode |

**Claude Code 2.1.296** (`claude --help`, and the headless docs at
<https://code.claude.com/docs/en/headless>, both read 2026-10-11):

- `claude -p` without `--bare` loads the same context as an interactive session (CLAUDE.md, hooks,
  skills, plugins, MCP).
- `--strict-mcp-config` with `--mcp-config` limits MCP servers to the ones passed.
- `--permission-prompts none` removes tools that need a person, such as `AskUserQuestion`.
- `--max-budget-usd` caps spend; JSON output reports `total_cost_usd`.

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
  --strict-mcp-config --mcp-config safe-servers.json \
  --permission-prompts none \
  --max-budget-usd <cap> --output-format stream-json \
  --model <model>
```

No outbound MCP server (Slack, Gmail, Salesforce, monday) ever connects; no `git push`; the working
directory is a throwaway copy. A variant that tests an MCP server as a harness fix connects only that
server.

---

## 4. Validity guards

1. **Oracle leak check.** An oracle may add only information its actor could legitimately have had.
   A restated task or improved instruction that contains answer-specific details from the human's
   follow-ups is rejected before it runs; otherwise verdicts drift toward Task and Agent setup.
2. **Judge calibration.** The judge's pass/fail is compared with the human's labels on ~20 replays
   before any verdict counts; its agreement is reported with every result.
3. **Baseline before variants.** Every episode is replayed unchanged N times first; that baseline is
   what makes "Not reproducible" detectable.
4. **Planted-fault accuracy.** selftest breaks one layer on purpose — a removed tool (Harness), a vague
   prompt (Agent setup), a weak model (Model), a broken check (Misjudged) — and reports how often culprit
   names it. That figure is printed beside every verdict.

## 5. Known limits, reported rather than hidden

- Episodes in which Claude asked the human a question cannot be replayed faithfully (no person to
  answer); they are tagged and excluded from v1.
- Episodes already run on Fable cannot test the Model cause; reported as untestable.
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
