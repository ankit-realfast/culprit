"""The decision rules and the stored verdict: one cause per episode, with the pass rates that prove it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.experiment import VariantOutcome
from culprit.stats import fisher_one_sided

SIGNIFICANCE = 0.05
VERDICT_FILE = "verdict.json"
BASELINE = "baseline"  # the unchanged replay
COMBINED = "combined"  # every tested change at once


@dataclass(frozen=True)
class Verdict:
    episode_id: str
    symptom: str
    cause: str
    evidence: str
    outcomes: dict[str, dict]
    untested: dict[str, str]
    pair: tuple[str, ...] = ()


def beats(variant: VariantOutcome, baseline: VariantOutcome) -> bool:
    return fisher_one_sided(variant.passes, variant.runs, baseline.passes, baseline.runs) < SIGNIFICANCE


def reproducible(baseline: VariantOutcome) -> bool:
    """The failure reproduces unless the unchanged replay passes most of the time."""
    return baseline.passes * 2 <= baseline.runs


def numbers_line(verdict: Verdict) -> str | None:
    """The pass counts behind a verdict, e.g. `harness: baseline 0/10 → 8/10, p = 0.0004`; None without replays."""
    baseline = verdict.outcomes.get(BASELINE)
    if baseline is None:
        return None
    candidates = ("+".join(verdict.pair), COMBINED) if verdict.cause == "multi-cause" else (verdict.cause,)
    p_of = {name: fisher_one_sided(verdict.outcomes[name]["passes"], verdict.outcomes[name]["runs"],
                                   baseline["passes"], baseline["runs"])
            for name in candidates if name in verdict.outcomes}
    # the variant that beat the baseline; with two changes the pair may have been run and lost to `combined`
    winner = next((name for name, p in p_of.items() if p < SIGNIFICANCE), None)
    label = verdict.cause if winner in (None, verdict.cause) else f"{verdict.cause} ({winner})"
    line = f"{label}: baseline {baseline['passes']}/{baseline['runs']}"
    if winner is None:
        return line
    won, p = verdict.outcomes[winner], p_of[winner]
    return f"{line} → {won['passes']}/{won['runs']}, p {'< 0.0001' if p < 0.0001 else f'= {p:.4f}'}"


def save_verdict(folder: Path, verdict: Verdict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / VERDICT_FILE
    path.write_text(json.dumps(asdict(verdict), indent=2))
    return path


def load_verdict(folder: Path) -> Verdict | None:
    path = folder / VERDICT_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data["pair"] = tuple(data.get("pair") or ())
    return Verdict(**data)
