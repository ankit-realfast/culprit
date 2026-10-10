"""The report: what to change, ranked by how many failed episodes each change would have fixed.

Built from stored verdicts only. What to fix first (single-layer fixes ranked by how many episodes each
covers), then causes that only work together, the episodes left for review, the symptom × cause grid, how far
the verdicts can be trusted, every verdict with its evidence, and what culprit could not see.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from culprit.selftest import SELFTEST_DIR
from culprit.store import load_restore
from culprit.taxonomy import Cause, Symptom
from culprit.verdict import load_verdict, numbers_line

REPORT_FILE = "report.md"
# what to change for each cause a single layer explains; the other causes are not fixes: unexplained is listed
# for human review, multi-cause under the causes that interact, misjudged and not-reproducible need nothing
FIX_ACTIONS = {
    "task": "how you ask: scope and success criteria in the prompt",
    "environment": "the repo or infrastructure the agent runs in",
    "agent-setup": "standing instructions: CLAUDE.md, rules, skills, subagent definitions",
    "harness": "Claude Code's setup: tools, MCP servers, hooks, settings, context handling",
    "model": "the model: --model, or route these tasks to Fable",
}
KNOWN_LIMITS = (
    "Restores are exact for files Claude Code edited; in git projects other files come from the last commit "
    "before the episode, elsewhere they are as they are today.",
    "Replays read a copy of the project's memory as it is today, not as it was then.",
    "MCP tools answered from the log, so a call the original never made failed; questions were answered with "
    "your recorded answers only when the replay asked something close.",
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
                            verdict.untested, load_restore(folder), numbers_line(verdict)))
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
        for cause, reason in row.untested.items():
            lines.append(f"  - {cause} untested: {reason}")
        if row.restore:
            r = row.restore
            lines.append(f"  - restore: snapshot agreed with the episode's record for {r['snapshot_agreed']} of "
                         f"{r['checked']} files; {r['unknown']} unknown, {r['missing_backups']} missing")
    lines += ["", "## Known limits", "", *[f"- {limit}" for limit in KNOWN_LIMITS]]
    return "\n".join(lines) + "\n"
