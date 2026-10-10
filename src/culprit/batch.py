"""Diagnose every prepared episode in the triage queue, sharing one total budget.

An episode that hits its own cap is set aside and the batch goes on; when the total runs out the batch
stops and names the next episode, and rerunning resumes, because finished verdicts are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from culprit.claude import CALL_CAP_USD, ClaudeCallFailed
from culprit.criterion import criterion_step, load_criterion
from culprit.diagnose import diagnose
from culprit.experiment import BudgetExhausted, EpisodeBudget, Progress, pristine_dir, scoped, stored_spend
from culprit.logs import EpisodeNotFound, find_episode, follow_up_prompts
from culprit.replay import SetupScriptFailed, baseline_settings
from culprit.store import (SELFTEST_DIR, confirm_step, display_id, episode_dir, queued_ids, read_symptom, require_queue,
                           short_id)
from culprit.verdict import load_verdict

ENV_FIX_FILE = "env-fix.sh"


@dataclass
class BatchSummary:
    diagnosed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    over_cap: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    spent_usd: float = 0.0


def env_fix_script(folder: Path) -> Path | None:
    """The environment fix stored with `culprit env-fix`, if any."""
    script = folder / ENV_FIX_FILE
    return script if script.exists() else None


def total_spend(runs_dir: Path) -> float:
    """What every episode in runs/ has spent, selftests aside: the batch total covers all of it."""
    if not runs_dir.is_dir():
        return 0.0
    return sum(stored_spend(folder) for folder in runs_dir.iterdir() if folder.is_dir() and folder.name != SELFTEST_DIR)


def readiness_problem(runs_dir: Path, episode_id: str) -> str | None:
    shown = display_id(runs_dir, episode_id)
    symptom = read_symptom(runs_dir, episode_id)
    if symptom is None:
        return f"not confirmed — {confirm_step(runs_dir, episode_id)}"
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None or not criterion.approved:
        return f"no approved criterion — {criterion_step(criterion, shown, symptom.value)}"
    if not pristine_dir(runs_dir, episode_id).is_dir():
        return f"not restored — `culprit diagnose {shown}` restores it or says why it cannot"
    return None


def diagnose_all(runs_dir: Path, projects_dir: Path, *, n: int, per_episode_usd: float, total_usd: float,
                 replay_budget_usd: float, diagnose_fn=diagnose, find=find_episode,
                 follow=follow_up_prompts, progress: Progress | None = None) -> BatchSummary:
    summary = BatchSummary()
    smallest_useful_usd = replay_budget_usd + CALL_CAP_USD  # one replay and its judgement
    require_queue(runs_dir)
    queued = queued_ids(runs_dir)
    # the total covers every batch ever run in this folder, not just this one
    remaining = total_usd - total_spend(runs_dir)
    for episode_id in queued:
        folder = episode_dir(runs_dir, episode_id)
        if load_verdict(folder) is not None:
            summary.skipped[episode_id] = "already diagnosed"
            continue
        problem = readiness_problem(runs_dir, episode_id)
        if problem:
            summary.skipped[episode_id] = problem
            continue
        if remaining < smallest_useful_usd:
            summary.stopped_at = episode_id
            break
        already = stored_spend(folder)
        cap = min(per_episode_usd, already + remaining)
        budget = EpisodeBudget(cap, spent_usd=already)
        stop = False
        try:
            episode = find(projects_dir, episode_id)
            diagnose_fn(episode, load_criterion(runs_dir, episode_id), read_symptom(runs_dir, episode_id),
                        baseline_settings(episode, replay_budget_usd), runs_dir=runs_dir,
                        follow_ups=follow(projects_dir, episode_id),
                        env_script=env_fix_script(folder), n=n, budget=budget,
                        progress=scoped(progress, short_id(episode_id)))
            summary.diagnosed.append(episode_id)
        except BudgetExhausted:
            if cap < per_episode_usd:  # the total ran out, not this episode's own cap
                summary.stopped_at = episode_id
                stop = True
            else:
                summary.over_cap.append(episode_id)
        except (ClaudeCallFailed, SetupScriptFailed, EpisodeNotFound) as error:
            summary.skipped[episode_id] = f"failed: {error}"
        spent_now = budget.spent_usd - already
        summary.spent_usd += spent_now
        remaining -= spent_now
        if stop:
            break
    return summary
