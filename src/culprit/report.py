"""What culprit tells you: the report across every diagnosed episode, and where each episode stands."""

from __future__ import annotations

import shutil
import textwrap
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from culprit.diagnose import (BASELINE, VariantOutcome, Verdict, load_instructions_draft, load_outcome, load_outcomes,
                              load_verdict, costs_line, limits_line, models_line, network_line, numbers_line, pristine_dir, stored_spend)
from culprit.logs import WhatHappened
from culprit.model import CRITERION_FILE, Criterion, Suggestion, criterion_step, load_criterion
from culprit.store import (SELFTEST_DIR, SYMPTOM_MEANINGS, Cause, Symptom, confirm_step, display_id, episode_dir,
                           known_episode_ids, load_restore, prompt_excerpt, queued_ids, read_symptom)


# --- report: The report: what to change, ranked by how many failed episodes each change would have fixed. ---
# Built from stored verdicts only. What to fix first (single-layer fixes ranked by how many episodes each
# covers), then causes that only work together, the episodes left for review, the symptom × cause grid, how far
# the verdicts can be trusted, every verdict with its evidence, and what culprit could not see.

REPORT_FILE = "report.md"
# what to change for each cause a single layer explains; the other causes are not fixes: unexplained is listed
# for human review, multi-cause under the causes that interact, misjudged and not-reproducible need nothing
FIX_ACTIONS = {
    "task": "how you ask: scope and success criteria in the prompt",
    "environment": "the repo or infrastructure the agent runs in",
    "agent-setup": "standing instructions: CLAUDE.md, rules, skills, subagent definitions",
    "harness": "Claude Code's setup: tools, MCP servers, hooks, settings, context handling",
    "model": "the model or its effort: --effort max where the verdict says raise effort, the latest of the same "
             "family where it says upgrade, else --model or route these tasks to Fable",
}
KNOWN_LIMITS = (
    "Restores are exact for files Claude Code edited; in git projects other files come from the last commit "
    "before the episode, elsewhere they are as they are today.",
    "Replays read a copy of the project's memory as it is today, not as it was then.",
    "MCP tools answered from the log, so a call the original never made failed; questions were answered with "
    "your recorded answers only when the replay asked something close.",
    "Replays were offline: a network shell command or web call got its recorded result only when identical to one "
    "the episode made, and was refused otherwise (counted under each verdict as served and refused).",
    "Causes marked untested below were not measured for that episode; a cheaper fix may exist there.",
    "A harness verdict says what made it pass, not whether the fix is yours or Claude Code's.",
    "Replays start from the start of the episode, not from the step where it went wrong.",
)
CAUSE_RANK = {cause.value: rank for rank, cause in enumerate(Cause)}
SYMPTOM_ROWS = [s.value for s in Symptom]


@dataclass(frozen=True)
class Row:
    episode_id: str
    symptom: str
    cause: str
    evidence: str
    pair: tuple[str, ...]
    untested: dict[str, str]
    restore: dict | None = None
    numbers: str | None = None  # the pass counts and p behind the verdict
    instructions: tuple[str, dict] | None = None  # agent-setup: the tested lines and the drafter's note
    limits: str | None = None  # how many counted runs per variant stopped at a limit
    costs: str | None = None  # each variant's cost per run, in dollars and tokens
    models: str | None = None  # which model each variant actually ran as
    network: str | None = None  # network calls per variant: served from the log, refused as unrecorded


@dataclass(frozen=True)
class Fix:
    cause: str
    count: int
    episodes: tuple[str, ...]
    action: str


@dataclass(frozen=True)
class Interaction:
    layers: str  # e.g. "task + model", in cause order
    count: int
    episodes: tuple[str, ...]


def collect(runs_dir: Path) -> list[Row]:
    rows = []
    for folder in sorted(p for p in runs_dir.iterdir() if p.is_dir() and p.name != SELFTEST_DIR) if runs_dir.is_dir() else []:
        verdict = load_verdict(folder)
        if verdict is not None:
            rows.append(Row(verdict.episode_id, verdict.symptom, verdict.cause, verdict.evidence, verdict.pair,
                            verdict.untested, load_restore(folder), numbers_line(verdict),
                            load_instructions_draft(folder) if verdict.cause == Cause.AGENT_SETUP.value else None,
                            limits_line(verdict), costs_line(verdict), models_line(verdict), network_line(verdict)))
    return rows


def grid(rows: list[Row]) -> dict[tuple[str, str], int]:
    return dict(Counter((row.symptom, row.cause) for row in rows))


def fix_list(rows: list[Row]) -> list[Fix]:
    by_cause: dict[str, list[Row]] = {}
    for row in rows:
        if row.cause in FIX_ACTIONS:
            by_cause.setdefault(row.cause, []).append(row)
    fixes = [Fix(cause, len(members), tuple(row.episode_id for row in members), FIX_ACTIONS[cause])
             for cause, members in by_cause.items()]
    return sorted(fixes, key=lambda fix: (-fix.count, CAUSE_RANK[fix.cause]))


def interactions(rows: list[Row]) -> list[Interaction]:
    """Multi-cause verdicts grouped by the layers that had to change together, most episodes first."""
    by_layers: dict[tuple[str, ...], list[str]] = {}
    for row in rows:
        if row.cause == "multi-cause":
            by_layers.setdefault(tuple(sorted(row.pair, key=CAUSE_RANK.__getitem__)), []).append(row.episode_id)
    ranked = sorted(by_layers.items(), key=lambda item: (-len(item[1]), [CAUSE_RANK[c] for c in item[0]]))
    return [Interaction(" + ".join(layers), len(members), tuple(members)) for layers, members in ranked]


def render_report(rows: list[Row], trust: list[str]) -> str:
    lines = ["# culprit report", ""]
    if not rows:
        lines += ["No verdicts yet. Rank failures with `culprit triage`, confirm them and approve what success means "
                  "with `culprit confirm`, then run `culprit diagnose` (one episode, or every one with no id).",
                  "", "## How far to trust this", "", *[f"- {line}" for line in trust]]
        return "\n".join(lines) + "\n"
    fixes, together = fix_list(rows), interactions(rows)
    lines += ["## What to fix first", ""]
    if fixes:
        top = fixes[0]
        lines.append(f"**Change {top.cause} first:** it explains {top.count} of {len(rows)} diagnosed episodes — "
                     f"change {top.action}.")
    elif together:
        top = together[0]
        lines.append(f"**Change {top.layers} together first:** together they explain {top.count} of {len(rows)} "
                     f"diagnosed episodes, and no single layer did.")
    else:
        lines.append(f"**No single change to make:** none of the {len(rows)} diagnosed episodes was explained by "
                     f"one layer.")
    if fixes:
        lines += ["", "| Fix | Episodes | What you change |", "|---|---|---|"]
        lines += [f"| {fix.cause} | {fix.count} | {fix.action} |" for fix in fixes]
    if together:
        lines += ["", "## Causes that interact", "",
                  "No single change made these pass; changing these layers together did.", ""]
        lines += [f"- **{i.layers}** — {i.count} episode{'s' if i.count != 1 else ''}: {', '.join(i.episodes)}"
                  for i in together]
    unexplained = [row for row in rows if row.cause == "unexplained"]
    if unexplained:
        lines += ["", "## Needs your review", "",
                  *[f"- **{row.episode_id}** — {row.evidence}" for row in unexplained]]
    cells = grid(rows)
    causes = [c.value for c in Cause if any(cause == c.value for _, cause in cells)]
    symptoms = [s for s in SYMPTOM_ROWS if any(symptom == s for symptom, _ in cells)]
    lines += ["", "## Symptom × cause", "", "| Symptom | " + " | ".join(causes) + " |",
              "|---|" + "---|" * len(causes)]
    for symptom in symptoms:
        lines.append(f"| {symptom} | " + " | ".join(str(cells.get((symptom, c), "")) for c in causes) + " |")
    lines += ["", "## How far to trust this", "", *[f"- {line}" for line in trust]]
    lines += ["", "## Verdicts", ""]
    for row in rows:
        lines.append(f"- **{row.episode_id}** — {row.symptom} → **{row.cause}**: {row.evidence}")
        if row.numbers:
            lines.append(f"  - {row.numbers}")
        if row.limits:
            lines.append(f"  - {row.limits}")
        if row.network:
            lines.append(f"  - {row.network}")
        if row.costs:
            lines.append(f"  - {row.costs}")
        if row.models:
            lines.append(f"  - {row.models}")
        if row.instructions:
            text, note = row.instructions
            lines.append(f"  - tested: adding these lines to {note.get('written_to') or 'the project CLAUDE.md'} "
                         f"made it pass:")
            lines += [f"    > {line}" for line in text.splitlines()]
            if note.get("placement"):
                lines.append(f"  - suggested placement (not tested): {note['placement']}"
                             f"{' — ' + note['reason'] if note.get('reason') else ''}")
        for cause, reason in row.untested.items():
            lines.append(f"  - {cause} untested: {reason}")
        if row.restore:
            r = row.restore
            lines.append(f"  - restore: snapshot agreed with the episode's record for {r['snapshot_agreed']} of "
                         f"{r['checked']} files; {r['unknown']} unknown, {r['missing_backups']} missing")
    lines += ["", "## Known limits", "", *[f"- {limit}" for limit in KNOWN_LIMITS]]
    return "\n".join(lines) + "\n"


# --- status: Where each episode stands, read from the files in runs/, and the command that moves it on. ---

STAGES = ("not queued", "queued", "confirmed", "criterion drafted", "criterion approved", "restored", "baselined",
          "diagnosed")


@dataclass(frozen=True)
class EpisodeStatus:
    episode_id: str
    stage: str  # one of STAGES
    next_step: str  # what to run next, in words, naming the command
    symptom: str | None = None
    criterion: Criterion | None = None
    restore: dict | None = None  # restore's trust numbers (restore.json)
    outcomes: tuple[VariantOutcome, ...] = ()  # every variant measured so far
    spent_usd: float = 0.0
    verdict: Verdict | None = None


def episode_status(runs_dir: Path, episode_id: str) -> EpisodeStatus:
    folder = episode_dir(runs_dir, episode_id)
    queued = episode_id in queued_ids(runs_dir)
    symptom = read_symptom(runs_dir, episode_id)
    criterion = load_criterion(runs_dir, episode_id)
    verdict = load_verdict(folder)
    shown = display_id(runs_dir, episode_id)
    symptom_value = symptom.value if symptom else None
    diagnose = f"run `culprit diagnose {shown}`"  # it restores and runs the baseline before the oracles
    steps = [  # (stage reached once done, done?, what to run when it is not)
        ("confirmed", symptom is not None, lambda: confirm_step(runs_dir, episode_id)),
        ("criterion drafted", criterion is not None, lambda: criterion_step(criterion, shown, symptom_value)),
        ("criterion approved", bool(criterion and criterion.approved),
         lambda: criterion_step(criterion, shown, symptom_value)),
        ("restored", pristine_dir(runs_dir, episode_id).is_dir(), lambda: diagnose),
        ("baselined", load_outcome(folder, BASELINE) is not None, lambda: diagnose),
        ("diagnosed", verdict is not None, lambda: diagnose),
    ]
    stage, hint = ("queued" if queued else "not queued"), None
    if verdict is not None:  # a verdict can come without a baseline (no-failure), so it settles the stage alone
        stage = "diagnosed"
    else:
        for reached, done, step in steps:
            if not done:
                hint = step()
                break
            stage = reached
    return EpisodeStatus(episode_id, stage, hint or "run `culprit report`", symptom_value,
                         criterion, load_restore(folder), load_outcomes(folder), stored_spend(folder), verdict)


def next_step(runs_dir: Path, episode_id: str) -> str:
    return episode_status(runs_dir, episode_id).next_step


def queue_status(runs_dir: Path) -> list[EpisodeStatus]:
    """Every episode in the triage queue (queue order), then every other episode with a folder."""
    return [episode_status(runs_dir, episode_id) for episode_id in known_episode_ids(runs_dir)]


# --- text: How every command lays out what it prints for a person. ---
# One block in reading order: the episode named once at the top, content, file paths last and shortened, then
# one numbered Next section. Headings in plain words, lists numbered, long text wrapped and indented, ✓ for a
# step done; no colour.

TEXT_WIDTH = 100  # when the terminal's width is unknown (a pipe, a file)
MIN_TEXT_WIDTH = 40
DONE = "✓"
NO_CHECKS = "none — add test commands (e.g. npm test) so replays are judged by tests"


@dataclass(frozen=True)
class Step:
    """One numbered step of a Next section: what to do, and the command that does it (None when there is none)."""
    words: str
    command: str | None = None


def text_width() -> int:
    return max(shutil.get_terminal_size((TEXT_WIDTH, 24)).columns, MIN_TEXT_WIDTH)


def wrap(text: str, indent: str = "", first: str | None = None) -> list[str]:
    """text wrapped to the terminal; every line starts with indent, the first with first when given (a number)."""
    first = indent if first is None else first
    rest = indent if first == indent else " " * len(first)
    return textwrap.wrap(text, text_width(), initial_indent=first, subsequent_indent=rest, break_long_words=False,
                         break_on_hyphens=False) or [first.rstrip()]


def numbered(items, indent: str = "    ") -> list[str]:
    return [line for i, item in enumerate(items, 1) for line in wrap(item, first=f"{indent}{i}. ")]


def block(heading: str, lines: list[str]) -> list[str]:
    """A section: a blank line, the heading, its indented lines."""
    return ["", heading, *lines]


def next_section(steps: list[Step]) -> list[str]:
    """The closing Next section; a command never breaks across lines, it moves to its own line instead."""
    lines = ["", "Next"]
    for i, step in enumerate(steps, 1):
        number = f"  {i}. "
        one_line = f"{number}{step.words}:  {step.command}" if step.command else None
        if one_line and len(one_line) <= text_width():
            lines.append(one_line)
        elif step.command:
            lines += wrap(f"{step.words}:", first=number) + [" " * len(number) + step.command]
        else:
            lines += wrap(step.words, first=number)
    return lines


def episode_header(runs_dir: Path, episode_id: str, ref: str) -> list[str]:
    """`Episode 4 · 50c0a213:b0567c84` and the prompt's excerpt: the one place a command names the episode."""
    shown = display_id(runs_dir, episode_id)
    lines = [f"Episode {ref} · {shown}" if ref != shown else f"Episode {shown}"]
    if excerpt := prompt_excerpt(runs_dir, episode_id):
        lines += wrap(f'"{excerpt}"', "  ")
    return lines


def shown_runs(runs_dir: Path) -> Path:
    """runs/ as the person would type it: relative to the current folder when it is inside it."""
    try:
        return runs_dir.resolve().relative_to(Path.cwd().resolve()) if runs_dir.is_absolute() else runs_dir
    except ValueError:
        return runs_dir


def short_path(runs_dir: Path, episode_id: str, name: str = "") -> str:
    """runs/<first 8 of the session>…<first 8 of the prompt>/name: a file in the episode's folder, short enough to
    read (the folder itself is the full id); folders already that short are shown as they are."""
    session, _, prompt = episode_id.partition(":")
    folder = episode_dir(runs_dir, episode_id).name
    if len(session) > 8 or len(prompt) > 8:
        folder = f"{session[:8]}…{prompt[:8]}"
    path = shown_runs(runs_dir) / folder
    return str(path / name if name else path)


RECAP_LABEL_WIDTH = len("You said next")


def what_happened_block(recap: WhatHappened) -> list[str]:
    """The episode in plain words, labels in one column and every value wrapped under its own start."""
    lines = ["What happened"]
    for label, values in recap.rows():
        first = f"  {label:<{RECAP_LABEL_WIDTH}}  "
        lines += wrap(values[0], first=first)
        lines += [line for value in values[1:] for line in wrap(value, " " * len(first))]
    return lines


def suggestion_lines(suggestion: Suggestion) -> list[str]:
    """`Suggested: wrong-result — <reason>`, set apart under What happened."""
    return ["", *wrap(f"Suggested: {suggestion.symptom.value} — {suggestion.reason}", "  ", first="")]


def symptoms_block(numbered: bool = False) -> list[str]:
    """The five symptoms, each with its one-line meaning; numbered as the question that follows numbers them."""
    width = max(len(symptom.value) for symptom in SYMPTOM_MEANINGS)
    lines = ["", "Symptoms"]
    for i, (symptom, meaning) in enumerate(SYMPTOM_MEANINGS.items(), 1):
        first = f"  {f'{i} ' if numbered else ''}{symptom.value:<{width}}  "
        lines += wrap(meaning, first=first)
    return lines


def criterion_block(criterion: Criterion) -> list[str]:
    heading = "Success criterion — approved" if criterion.approved else "Draft success criterion — not approved yet"
    return [heading, "  Success means", *wrap(criterion.success_means, "    "),
            "  Must not", *(numbered(criterion.must_not) or ["    none"]),
            "  Checks", *(numbered(criterion.checks) or wrap(NO_CHECKS, "    "))]


def verdict_lines(verdict: Verdict) -> list[str]:
    """The verdict as `status` and `diagnose` show it: the cause, the evidence behind it, what was not tested."""
    evidence = [verdict.evidence] + [line for line in (numbers_line(verdict), limits_line(verdict),
                                                       network_line(verdict), costs_line(verdict),
                                                       models_line(verdict)) if line]
    lines = ["Verdict", f"  Cause: {verdict.cause}", "  Evidence", *(l for e in evidence for l in wrap(e, "    "))]
    if verdict.pair:
        lines.append(f"  Changed together: {' + '.join(verdict.pair)}")
    if verdict.untested:
        lines += ["  Not tested", *numbered(f"{cause}: {reason}" for cause, reason in verdict.untested.items())]
    return lines


def trust_block(trust: list[str]) -> list[str]:
    """How far to trust the verdicts (trust_lines in the CLI), as a section for a person."""
    lines = ["How far to trust the verdicts"]
    for line in trust:
        head, _, state = line.partition(" — ")  # the judge's calibration state goes on its own line
        lines += wrap(head[:1].upper() + head[1:], "  ") + (wrap(state, "    ") if state else [])
    return lines


def progress_lines(runs_dir: Path, status: EpisodeStatus) -> list[str]:
    """Each step of the pipeline, ✓ once done."""
    steps = [("Queued", status.episode_id in queued_ids(runs_dir)),
             (f"What went wrong: {status.symptom}" if status.symptom else "What went wrong", bool(status.symptom)),
             ("Success criterion drafted", status.criterion is not None),
             ("Success criterion approved", bool(status.criterion and status.criterion.approved)),
             ("Starting files restored", pristine_dir(runs_dir, status.episode_id).is_dir()),
             ("Baseline replayed", any(o.variant == BASELINE for o in status.outcomes)),
             ("Diagnosed", status.verdict is not None)]
    return [f"  {DONE if done else ' '} {words}" for words, done in steps]


def episode_steps(runs_dir: Path, status: EpisodeStatus, ref: str) -> list[Step]:
    """What to do next with one episode, in order, naming the episode as ref."""
    confirm = Step("Say what went wrong (one of " + ", ".join(s.value for s in Symptom) + ")",
                   f"culprit confirm {ref} <symptom>")
    if status.stage == "not queued":
        return [Step("Put it in the queue: rank failures until it is listed", "culprit triage"), confirm]
    return {"queued": [confirm],
            "confirmed": [Step("Draft what success means", f"culprit confirm {ref}")],
            "criterion drafted": [
                Step(f"Read the draft. To change it, edit {short_path(runs_dir, status.episode_id, CRITERION_FILE)}"),
                Step("Approve it", f"culprit confirm {ref} approve")],
            "diagnosed": [Step("Rank the changes across episodes", "culprit report")],
            }.get(status.stage, [Step("Diagnose it", f"culprit diagnose {ref}")])


def row_command(status: EpisodeStatus, ref: str) -> str:
    """The one command that moves an episode on, for its row in the `status` list."""
    return {"not queued": "culprit triage", "queued": f"culprit confirm {ref} <symptom>",
            "confirmed": f"culprit confirm {ref}", "criterion drafted": f"culprit confirm {ref}",
            "diagnosed": "culprit report"}.get(status.stage, f"culprit diagnose {ref}")


def queue_steps(statuses: list[EpisodeStatus]) -> list[Step]:
    """The Next section under the `status` list: the walk, the batch, the report, as far as the queue has got."""
    stages = {status.stage for status in statuses}
    steps = []
    if stages & {"queued", "confirmed", "criterion drafted"}:
        steps.append(Step("Confirm the queue, episode by episode", "culprit confirm"))
    if stages & {"criterion approved", "restored", "baselined"}:
        steps.append(Step("Diagnose every confirmed episode", "culprit diagnose"))
    if "diagnosed" in stages:
        steps.append(Step("Rank the changes across episodes", "culprit report"))
    return steps + [Step("See one episode in full", "culprit status <row>")]
