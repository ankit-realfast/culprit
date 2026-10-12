"""How far culprit can be trusted: the judge calibrated against human labels, and selftest's planted faults."""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from culprit.diagnose import (BASELINE, EMPTY, JUDGEMENT_FILE, REPLAYS_DIR, CostPlan, EpisodeBudget, Progress, diagnose,
                              diagnose_cost_for, known_oracle_id, load_outcome, load_verdict, scoped, stored_spend)
from culprit.logs import Episode
from culprit.model import Criterion
from culprit.replay import DIFF_FILE, RESULT_FILE, ReplaySettings
from culprit.store import SELFTEST_DIR, Cause, Symptom, episode_dir, read_json, write_json


# --- calibration ---
# How far the judge can be trusted: the human labels real replays and Cohen's kappa measures agreement.
# Verdicts are marked uncalibrated until at least MIN_LABELS replays are labelled and kappa reaches MIN_KAPPA.

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
    answer = "\n".join(f"  {line}" for line in (result.get("result_text") or "(none)").splitlines())
    diff = "\n".join(f"  {line}" for line in f"{preview}{more}".splitlines())
    return (f"Final answer\n{answer}\nThe judge said: {verdict} — {judgement['reason']}\n"
            f"{files_changed} file{'s' if files_changed != 1 else ''} changed" + (f"\n{diff}" if diff else ""))


# --- selftest ---
# culprit tested on itself: break one layer of a control episode on purpose and check diagnose names it.
# A control episode is one whose unchanged replay passes at least 4 in 5 of the N runs (8 of 10 at N = 10),
# so any new failure comes from the planted fault. The share of faults named correctly is culprit's accuracy.

CONTROL_MIN_SHARE = (4, 5)  # a control's baseline passes at least 4 in 5 of the N runs
WEAK_MODEL = "haiku"  # the alias of the weakest tier; $CULPRIT_WEAK_MODEL pins another
WEAK_MODEL_ENV = "CULPRIT_WEAK_MODEL"
TRUNCATED_WORDS = 6
READ_ONLY_TOOLS = "Read,Grep,Glob"
MISLEADING_INSTRUCTION_TEXT = "Do not modify any files. Reply with a one-line summary of what you would do, then stop."
IMPOSSIBLE = Criterion(
    success_means="a file named CULPRIT_IMPOSSIBLE_MARKER exists and the check below passes",
    checks=("test -f CULPRIT_IMPOSSIBLE_MARKER && false",),  # can never exit 0: every run fails
    must_not=(),
    approved=True,
)


class PlantedFault(StrEnum):
    TRUNCATED_PROMPT = "truncated-prompt"
    MISLEADING_INSTRUCTION = "misleading-instruction"
    EDIT_TOOLS_REMOVED = "edit-tools-removed"
    WEAK_MODEL = "weak-model"
    IMPOSSIBLE_CRITERION = "impossible-criterion"


EXPECTED: dict[PlantedFault, Cause] = {
    PlantedFault.TRUNCATED_PROMPT: Cause.TASK,
    PlantedFault.MISLEADING_INSTRUCTION: Cause.AGENT_SETUP,
    PlantedFault.EDIT_TOOLS_REMOVED: Cause.HARNESS,
    PlantedFault.WEAK_MODEL: Cause.MODEL,
    PlantedFault.IMPOSSIBLE_CRITERION: Cause.UNEXPLAINED,
}


@dataclass(frozen=True)
class SelftestResult:
    fault: str
    expected: str
    got: str

    @property
    def correct(self) -> bool:
        return self.expected == self.got


def plant(fault: PlantedFault, base: ReplaySettings, criterion: Criterion) -> tuple[ReplaySettings, Criterion]:
    if fault is PlantedFault.TRUNCATED_PROMPT:
        return replace(base, prompt=" ".join(base.prompt.split()[:TRUNCATED_WORDS])), criterion
    if fault is PlantedFault.MISLEADING_INSTRUCTION:
        return replace(base, claude_md_addition=MISLEADING_INSTRUCTION_TEXT), criterion
    if fault is PlantedFault.EDIT_TOOLS_REMOVED:
        return replace(base, tools=READ_ONLY_TOOLS), criterion
    if fault is PlantedFault.WEAK_MODEL:
        return replace(base, model=os.environ.get(WEAK_MODEL_ENV) or WEAK_MODEL), criterion
    return base, IMPOSSIBLE


def control_min_passes(n: int) -> int:
    """How many of n baseline runs must pass for the episode to be a control (rounded up)."""
    share, of = CONTROL_MIN_SHARE
    return -(-n * share // of)


def is_control(runs_dir: Path, episode_id: str, n: int) -> bool:
    baseline = load_outcome(episode_dir(runs_dir, episode_id), BASELINE)
    return baseline is not None and baseline.passes >= control_min_passes(n)


def selftest_dir(runs_dir: Path, episode_id: str, fault: PlantedFault) -> Path:
    return runs_dir / SELFTEST_DIR / episode_dir(runs_dir, episode_id).name / fault.value


def run_selftest(episode: Episode, criterion: Criterion, base: ReplaySettings, *, runs_dir: Path,
                 follow_ups: list[str], n: int, budget_per_fault: float,
                 faults: tuple[PlantedFault, ...] = tuple(PlantedFault), diagnose_fn=diagnose,
                 progress: Progress | None = None) -> list[SelftestResult]:
    results = []
    for fault in faults:
        planted_base, planted_criterion = plant(fault, base, criterion)
        out_dir = selftest_dir(runs_dir, episode.episode_id, fault)
        verdict = diagnose_fn(episode, planted_criterion, Symptom.WRONG_RESULT, planted_base, runs_dir=runs_dir,
                              follow_ups=follow_ups, env_script=None, n=n,
                              budget=EpisodeBudget(budget_per_fault, spent_usd=stored_spend(out_dir)),
                              out_dir=out_dir, progress=scoped(progress, fault.value))
        results.append(SelftestResult(fault.value, EXPECTED[fault].value, verdict.cause))
    return results


def selftest_results(runs_dir: Path) -> list[SelftestResult]:
    results = []
    for fault_dir in sorted((runs_dir / SELFTEST_DIR).glob("*/*")):
        verdict = load_verdict(fault_dir)
        if verdict is not None and fault_dir.name in {f.value for f in PlantedFault}:
            results.append(SelftestResult(fault_dir.name, EXPECTED[PlantedFault(fault_dir.name)].value, verdict.cause))
    return results


def selftest_accuracy(runs_dir: Path) -> tuple[int, int]:
    results = selftest_results(runs_dir)
    return sum(r.correct for r in results), len(results)


def selftest_cost(runs_dir: Path, episode_id: str, criterion: Criterion, base: ReplaySettings, *, n: int,
                  budget_per_fault: float) -> CostPlan:
    """One diagnose per planted fault, each in its own folder under its own cap."""
    total = EMPTY
    for fault in PlantedFault:
        planted_base, planted_criterion = plant(fault, base, criterion)
        folder = selftest_dir(runs_dir, episode_id, fault)
        total += diagnose_cost_for(folder, planted_criterion, planted_base.max_budget_usd, model=planted_base.model,
                                   effort=planted_base.effort, oracle_id=known_oracle_id(runs_dir),
                                   env_fix=False, n=n, spent_usd=stored_spend(folder), cap_usd=budget_per_fault)
    return total
