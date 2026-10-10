"""How far the judge can be trusted: the human labels real replays and Cohen's kappa measures agreement.

Verdicts are marked uncalibrated until at least MIN_LABELS replays are labelled and kappa reaches MIN_KAPPA.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from culprit.diagnose import JUDGEMENT_FILE, REPLAYS_DIR
from culprit.replay import DIFF_FILE, RESULT_FILE
from culprit.store import read_json, write_json

HUMAN_LABEL_FILE = "human_label.json"
MIN_LABELS = 20
MIN_KAPPA = 0.6
DIFF_PREVIEW_LINES = 40


class UnknownRun(LookupError):
    pass


@dataclass(frozen=True)
class Agreement:
    labelled: int
    agreed: int
    kappa: float | None

    @property
    def calibrated(self) -> bool:
        return self.labelled >= MIN_LABELS and self.kappa is not None and self.kappa >= MIN_KAPPA


def judged_runs(runs_dir: Path) -> list[Path]:
    return sorted(path.parent for path in runs_dir.glob(f"*/{REPLAYS_DIR}/*/*/{JUDGEMENT_FILE}"))


def run_id(runs_dir: Path, run_dir: Path) -> str:
    return str(run_dir.relative_to(runs_dir))


def record_label(runs_dir: Path, run: str, passed: bool) -> Path:
    run_dir = runs_dir / run
    if not (run_dir / JUDGEMENT_FILE).is_file():
        raise UnknownRun(f"no judged replay at {run} under {runs_dir}")
    return write_json(run_dir / HUMAN_LABEL_FILE, {"passed": passed})


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(judge == human for judge, human in pairs) / n
    judge_yes = sum(judge for judge, _ in pairs) / n
    human_yes = sum(human for _, human in pairs) / n
    expected = judge_yes * human_yes + (1 - judge_yes) * (1 - human_yes)
    if expected == 1:
        return None  # both raters gave one value every time: agreement beyond chance is not measurable
    return (observed - expected) / (1 - expected)


def agreement(runs_dir: Path) -> Agreement:
    pairs = []
    for run_dir in judged_runs(runs_dir):
        label = run_dir / HUMAN_LABEL_FILE
        if label.is_file():
            judge = read_json(run_dir / JUDGEMENT_FILE)["passed"]
            pairs.append((bool(judge), bool(read_json(label)["passed"])))
    return Agreement(len(pairs), sum(j == h for j, h in pairs), cohens_kappa(pairs))


def next_unlabelled(runs_dir: Path, skip: Collection[Path] = ()) -> Path | None:
    """The first judged run with no human label, passing over the runs in skip."""
    return next((run_dir for run_dir in judged_runs(runs_dir)
                 if not (run_dir / HUMAN_LABEL_FILE).exists() and run_dir not in skip), None)


def run_summary(run_dir: Path) -> str:
    judgement = read_json(run_dir / JUDGEMENT_FILE)
    result = read_json(run_dir / RESULT_FILE)
    diff_lines = (run_dir / DIFF_FILE).read_text(errors="replace").splitlines()
    files_changed = sum(1 for line in diff_lines if line.startswith("+++ "))
    verdict = "pass" if judgement["passed"] else "fail"
    preview = "\n".join(diff_lines[:DIFF_PREVIEW_LINES])
    more = f"\n… {len(diff_lines) - DIFF_PREVIEW_LINES} more diff lines" if len(diff_lines) > DIFF_PREVIEW_LINES else ""
    return (f"final answer: {result.get('result_text', '')}\n"
            f"judge: {verdict} — {judgement['reason']}\n"
            f"{files_changed} file{'s' if files_changed != 1 else ''} changed\n{preview}{more}")
