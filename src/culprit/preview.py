"""The most a replaying command can run and spend, worked out and printed before it starts.

Every count follows the real code path: each variant runs up to n + MAX_VOID_RETRIES replays, each replay is
capped at its settings' budget plus one judge call, and drafting a task or instructions costs model calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Sequence

from culprit.batch import ENV_FIX_FILE, readiness_problem
from culprit.criterion import Criterion, load_criterion
from culprit.diagnose import BASELINE, COMBINED, CRITERION_USED_FILE, VARIANTS_DIR, criterion_key
from culprit.experiment import MAX_VOID_RETRIES, load_outcome, stored_spend
from culprit.judge import JUDGE_CALL_CAP_USD
from culprit.oracles import FABLE_PREFIX, HARNESS_BUDGET_MULTIPLIER, MAX_DRAFT_ATTEMPTS
from culprit.replay import ReplaySettings
from culprit.selftest import SELFTEST_DIR, PlantedFault, plant, selftest_dir
from culprit.store import candidate_rows, episode_dir
from culprit.taxonomy import ORACLE_ORDER, Cause
from culprit.verdict import load_verdict

DRAFTED_CAUSES = (Cause.TASK, Cause.AGENT_SETUP)
CALLS_PER_DRAFT_ATTEMPT = 2  # the draft and its leak check


@dataclass(frozen=True)
class CostPlan:
    replays: int  # most replays the command can start
    calls: int  # most drafting and leak-check calls
    max_usd: float  # most it can add to what is already spent
    spent_usd: float  # already spent, counted against the caps
    cap_usd: float

    def __add__(self, other: CostPlan) -> CostPlan:
        return CostPlan(self.replays + other.replays, self.calls + other.calls,
                        round(self.max_usd + other.max_usd, 2), round(self.spent_usd + other.spent_usd, 4),
                        round(self.cap_usd + other.cap_usd, 2))

    def within(self, room_usd: float) -> CostPlan:
        """The same plan when only room_usd is left to spend overall."""
        return CostPlan(self.replays, self.calls, round(max(0.0, min(self.max_usd, room_usd)), 2), self.spent_usd,
                        self.cap_usd)

    def lines(self) -> list[str]:
        calls = f" and {self.calls} drafting calls" if self.calls else ""
        return [f"plan: up to {self.replays} replays{calls} (each variant: n runs plus up to {MAX_VOID_RETRIES} "
                f"void retries)",
                f"cost: at most ${self.max_usd:.2f} more — ${self.spent_usd + self.max_usd:.2f} in total with the "
                f"${self.spent_usd:.2f} already spent (cap ${self.cap_usd:.2f})"]


EMPTY = CostPlan(0, 0, 0.0, 0.0, 0.0)


def plan_cost(replay_caps: Sequence[float], *, n: int, calls: int = 0, spent_usd: float, cap_usd: float) -> CostPlan:
    """replay_caps holds one replay budget per variant still to run."""
    per_variant = n + MAX_VOID_RETRIES
    worst = sum((cap + JUDGE_CALL_CAP_USD) * per_variant for cap in replay_caps) + calls * JUDGE_CALL_CAP_USD
    return CostPlan(per_variant * len(replay_caps), calls, round(max(0.0, min(cap_usd - spent_usd, worst)), 2),
                    spent_usd, cap_usd)


def diagnose_cost(base_cap: float, oracles: Sequence[Cause], *, n: int, spent_usd: float, cap_usd: float,
                  done: frozenset[str] = frozenset(), drafts: int = 0) -> CostPlan:
    """Baseline, every oracle, and when two or more can run, all of them together and every pair."""
    def cap_for(causes) -> float:
        return base_cap * (HARNESS_BUDGET_MULTIPLIER if Cause.HARNESS in causes else 1)

    caps = {BASELINE: base_cap, **{cause.value: cap_for({cause}) for cause in oracles}}
    if len(oracles) >= 2:
        caps[COMBINED] = cap_for(oracles)
        caps.update({f"{a.value}+{b.value}": cap_for({a, b}) for a, b in combinations(oracles, 2)})
    return plan_cost([cap for name, cap in caps.items() if name not in done], n=n,
                     calls=drafts * MAX_DRAFT_ATTEMPTS * CALLS_PER_DRAFT_ATTEMPT, spent_usd=spent_usd, cap_usd=cap_usd)


def diagnose_cost_for(folder: Path, criterion: Criterion, base_cap: float, *, model: str | None, env_fix: bool,
                      n: int, spent_usd: float, cap_usd: float) -> CostPlan:
    """diagnose_cost for what is on disk: stored outcomes and drafted variants are reused, not paid again."""
    marker = folder / CRITERION_USED_FILE
    stale = marker.is_file() and json.loads(marker.read_text())["key"] != criterion_key(criterion)
    oracles, drafts = [], 0
    for cause in ORACLE_ORDER:
        stored = folder / VARIANTS_DIR / f"{cause.value}.json"
        data = json.loads(stored.read_text()) if stored.is_file() and not stale else None
        if data is not None and "untested" in data:
            continue
        if data is None:
            if cause is Cause.ENVIRONMENT and not env_fix:
                continue
            if cause is Cause.MODEL and (model or "").startswith(FABLE_PREFIX):
                continue
            drafts += cause in DRAFTED_CAUSES
        oracles.append(cause)
    names = [BASELINE, COMBINED, *(c.value for c in oracles),
             *(f"{a.value}+{b.value}" for a, b in combinations(oracles, 2))]
    done = frozenset() if stale else frozenset(name for name in names if load_outcome(folder, name) is not None)
    return diagnose_cost(base_cap, oracles, n=n, spent_usd=spent_usd, cap_usd=cap_usd, done=done, drafts=drafts)


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


def batch_cost(runs_dir: Path, *, n: int, per_episode_usd: float, total_usd: float,
               replay_budget_usd: float) -> CostPlan:
    """Every queued episode diagnose-all would take on, at worst case (any model), within the shared total."""
    spent = sum(stored_spend(folder) for folder in runs_dir.iterdir()
                if folder.is_dir() and folder.name != SELFTEST_DIR) if runs_dir.is_dir() else 0.0
    plan = EMPTY
    for row in candidate_rows(runs_dir):
        episode_id = row["summary"]["episode_id"]
        folder = episode_dir(runs_dir, episode_id)
        if load_verdict(folder) is not None or readiness_problem(runs_dir, episode_id):
            continue
        plan += diagnose_cost_for(folder, load_criterion(runs_dir, episode_id), replay_budget_usd, model=None,
                                  env_fix=(folder / ENV_FIX_FILE).exists(), n=n, spent_usd=stored_spend(folder),
                                  cap_usd=per_episode_usd)
    return CostPlan(plan.replays, plan.calls, round(max(0.0, min(plan.max_usd, total_usd - spent)), 2),
                    round(spent, 4), total_usd)
