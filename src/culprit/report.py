"""The report: what to change, ranked by how many failed episodes each change would have fixed.

Built from stored verdicts only. Answer first, then the fix list, the symptom × cause grid, every
verdict with its evidence, how far the verdicts can be trusted, and what culprit could not see.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from culprit.selftest import SELFTEST_DIR
from culprit.taxonomy import Cause, Symptom
from culprit.verdict import load_verdict

REPORT_FILE = "report.md"
NOT_FIXES = ("misjudged", "not-reproducible", "unexplained")  # unexplained is listed for human review instead
FIX_ACTIONS = {
    "misjudged": "your label — it was not a failure",
    "not-reproducible": "nothing — the failure did not recur",
    "task": "how you ask: scope and success criteria in the prompt",
    "environment": "the repo or infrastructure the agent runs in",
    "agent-setup": "standing instructions: CLAUDE.md, rules, skills, subagent definitions",
    "harness": "Claude Code's setup: tools, MCP servers, hooks, settings, context handling",
    "model": "the model: --model, or route these tasks to Fable",
    "multi-cause": "two layers together",
    "unexplained": "nothing single — review these by hand",
}
KNOWN_LIMITS = (
    "Restores are exact only for files Claude Code edited; every other file is as it is today.",
    "Replays start in a new working directory, so the original project's auto-memory was not loaded.",
    "Episodes that used an MCP tool or asked you a question were not replayed.",
    "Causes marked untested below were not measured for that episode; a cheaper fix may exist there.",
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


@dataclass(frozen=True)
class Fix:
    cause: str
    count: int
    episodes: tuple[str, ...]
    action: str


def collect(runs_dir: Path) -> list[Row]:
    rows = []
    for folder in sorted(p for p in runs_dir.iterdir() if p.is_dir() and p.name != SELFTEST_DIR) if runs_dir.is_dir() else []:
        verdict = load_verdict(folder)
        if verdict is not None:
            rows.append(Row(verdict.episode_id, verdict.symptom, verdict.cause, verdict.evidence, verdict.pair,
                            verdict.untested))
    return rows


def grid(rows: list[Row]) -> dict[tuple[str, str], int]:
    return dict(Counter((row.symptom, row.cause) for row in rows))


def fix_list(rows: list[Row]) -> list[Fix]:
    by_cause: dict[str, list[Row]] = {}
    for row in rows:
        if row.cause not in NOT_FIXES:
            by_cause.setdefault(row.cause, []).append(row)
    fixes = []
    for cause, members in by_cause.items():
        action = FIX_ACTIONS[cause]
        if cause == "multi-cause":
            pairs = sorted({" + ".join(row.pair) for row in members})
            action = f"{action}: {'; '.join(pairs)}"
        fixes.append(Fix(cause, len(members), tuple(row.episode_id for row in members), action))
    return sorted(fixes, key=lambda fix: (-fix.count, CAUSE_RANK[fix.cause]))


def render_report(rows: list[Row], trust: list[str]) -> str:
    lines = ["# culprit report", ""]
    if not rows:
        lines += ["No verdicts yet. Confirm failures with `culprit triage` and `culprit confirm`, prepare them with "
                  "`culprit criterion` and `culprit restore`, then run `culprit diagnose` or `culprit diagnose-all`.",
                  "", "## How far to trust this", "", *[f"- {line}" for line in trust]]
        return "\n".join(lines) + "\n"
    fixes = fix_list(rows)
    if fixes:
        top = fixes[0]
        lines.append(f"**Change {top.cause} first:** it explains {top.count} of {len(rows)} diagnosed episodes — "
                     f"change {top.action}.")
    else:
        lines.append(f"**No single change to make:** none of the {len(rows)} diagnosed episodes was explained by "
                     f"one layer.")
    lines += ["", "## Fix list", "", "| Fix | Episodes | What you change |", "|---|---|---|"]
    lines += [f"| {fix.cause} | {fix.count} | {fix.action} |" for fix in fixes]
    cells = grid(rows)
    causes = [c.value for c in Cause if any(cause == c.value for _, cause in cells)]
    symptoms = [s for s in SYMPTOM_ROWS if any(symptom == s for symptom, _ in cells)]
    unexplained = [row for row in rows if row.cause == "unexplained"]
    if unexplained:
        lines += ["", "## Needs your review", "",
                  *[f"- **{row.episode_id}** — {row.evidence}" for row in unexplained]]
    lines += ["", "## Symptom × cause", "", "| Symptom | " + " | ".join(causes) + " |",
              "|---|" + "---|" * len(causes)]
    for symptom in symptoms:
        lines.append(f"| {symptom} | " + " | ".join(str(cells.get((symptom, c), "")) for c in causes) + " |")
    lines += ["", "## Verdicts", ""]
    for row in rows:
        lines.append(f"- **{row.episode_id}** — {row.symptom} → **{row.cause}**: {row.evidence}")
        for cause, reason in row.untested.items():
            lines.append(f"  - {cause} untested: {reason}")
    lines += ["", "## How far to trust this", "", *[f"- {line}" for line in trust],
              "", "## Known limits", "", *[f"- {limit}" for limit in KNOWN_LIMITS]]
    return "\n".join(lines) + "\n"
