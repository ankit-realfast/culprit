"""Diagnose every prepared episode in the triage queue, sharing one total budget.

An episode that hits its own cap is set aside and the batch goes on; when the total runs out the batch
stops and names the next episode, and rerunning resumes, because finished verdicts are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from culprit.criterion import load_criterion
from culprit.diagnose import diagnose
from culprit.experiment import PRISTINE_DIR, BudgetExhausted, EpisodeBudget
from culprit.locate import find_episode, follow_up_prompts
from culprit.replay import baseline_settings
from culprit.store import CANDIDATES_FILE, episode_dir, read_symptom
from culprit.verdict import load_verdict

ENV_FIX_FILE = "env-fix.sh"
MIN_USEFUL_BUDGET_USD = 1.0


@dataclass
class BatchSummary:
    diagnosed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    over_cap: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    spent_usd: float = 0.0


def readiness_problem(runs_dir: Path, episode_id: str) -> str | None:
    if read_symptom(runs_dir, episode_id) is None:
        return f"not confirmed — run `culprit confirm {episode_id} <symptom>`"
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None or not criterion.approved:
        return f"no approved criterion — run `culprit criterion draft {episode_id}` then `culprit criterion approve {episode_id}`"
    if not (episode_dir(runs_dir, episode_id) / PRISTINE_DIR).is_dir():
        return f"not restored — run `culprit restore {episode_id}`"
    return None


def diagnose_all(runs_dir: Path, projects_dir: Path, *, n: int, per_episode_usd: float, total_usd: float,
                 replay_budget_usd: float, diagnose_fn=diagnose, find=find_episode,
                 follow=follow_up_prompts) -> BatchSummary:
    summary = BatchSummary()
    queued = [row["summary"]["episode_id"] for row in json.loads((runs_dir / CANDIDATES_FILE).read_text())]
    for episode_id in queued:
        if load_verdict(episode_dir(runs_dir, episode_id)) is not None:
            summary.skipped[episode_id] = "already diagnosed"
            continue
        problem = readiness_problem(runs_dir, episode_id)
        if problem:
            summary.skipped[episode_id] = problem
            continue
        remaining = total_usd - summary.spent_usd
        if remaining < MIN_USEFUL_BUDGET_USD:
            summary.stopped_at = episode_id
            break
        budget = EpisodeBudget(min(per_episode_usd, remaining))
        episode = find(projects_dir, episode_id)
        env_script = episode_dir(runs_dir, episode_id) / ENV_FIX_FILE
        try:
            diagnose_fn(episode, load_criterion(runs_dir, episode_id), read_symptom(runs_dir, episode_id),
                        baseline_settings(episode, replay_budget_usd), runs_dir=runs_dir,
                        follow_ups=follow(projects_dir, episode_id),
                        env_script=env_script if env_script.exists() else None, n=n, budget=budget)
            summary.diagnosed.append(episode_id)
        except BudgetExhausted:
            summary.over_cap.append(episode_id)
        summary.spent_usd += budget.spent_usd
    return summary
