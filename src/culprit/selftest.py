"""culprit tested on itself: break one layer of a control episode on purpose and check diagnose names it.

A control episode is one whose unchanged replay passes at least 4 in 5 of the N runs (8 of 10 at N = 10),
so any new failure comes from the planted fault. The share of faults named correctly is culprit's accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from culprit.criterion import Criterion
from culprit.diagnose import diagnose
from culprit.logs import Episode
from culprit.experiment import EpisodeBudget, Progress, load_outcome, scoped, stored_spend
from culprit.preview import EMPTY, CostPlan, diagnose_cost_for
from culprit.replay import ReplaySettings
from culprit.store import Cause, SELFTEST_DIR, Symptom, episode_dir
from culprit.verdict import BASELINE, load_verdict

CONTROL_MIN_SHARE = (4, 5)  # a control's baseline passes at least 4 in 5 of the N runs
WEAK_MODEL_ID = "claude-haiku-5-5"
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
        return replace(base, append_system_prompt=MISLEADING_INSTRUCTION_TEXT), criterion
    if fault is PlantedFault.EDIT_TOOLS_REMOVED:
        return replace(base, tools=READ_ONLY_TOOLS), criterion
    if fault is PlantedFault.WEAK_MODEL:
        return replace(base, model=WEAK_MODEL_ID), criterion
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
                                   env_fix=False, n=n, spent_usd=stored_spend(folder), cap_usd=budget_per_fault)
    return total
