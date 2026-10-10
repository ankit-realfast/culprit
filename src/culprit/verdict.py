"""The decision rules and the stored verdict: one cause per episode, with the pass rates that prove it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.experiment import VariantOutcome
from culprit.stats import fisher_one_sided

SIGNIFICANCE = 0.05
VERDICT_FILE = "verdict.json"


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
