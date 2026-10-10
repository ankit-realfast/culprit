"""Diagnose every prepared episode in the triage queue, sharing one total budget.

An episode that hits its own cap is set aside and the batch goes on; when the total runs out the batch
stops and names the next episode, and rerunning resumes, because finished verdicts are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from culprit.claude import ClaudeCallFailed
from culprit.criterion import load_criterion
from culprit.diagnose import diagnose
from culprit.experiment import PRISTINE_DIR, BudgetExhausted, EpisodeBudget, stored_spend
from culprit.judge import JUDGE_CALL_CAP_USD
from culprit.locate import EpisodeNotFound, find_episode, follow_up_prompts
from culprit.replay import SetupScriptFailed, baseline_settings
from culprit.selftest import SELFTEST_DIR
from culprit.store import CANDIDATES_FILE, episode_dir, read_symptom
from culprit.verdict import load_verdict

ENV_FIX_FILE = "env-fix.sh"


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
    smallest_useful_usd = replay_budget_usd + JUDGE_CALL_CAP_USD  # one replay and its judgement
    queued = [row["summary"]["episode_id"] for row in json.loads((runs_dir / CANDIDATES_FILE).read_text())]
    # the total covers every batch ever run in this folder, not just this one
    remaining = total_usd - sum(stored_spend(folder) for folder in runs_dir.iterdir()
                                if folder.is_dir() and folder.name != SELFTEST_DIR)
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
            env_script = folder / ENV_FIX_FILE
            diagnose_fn(episode, load_criterion(runs_dir, episode_id), read_symptom(runs_dir, episode_id),
                        baseline_settings(episode, replay_budget_usd), runs_dir=runs_dir,
                        follow_ups=follow(projects_dir, episode_id),
                        env_script=env_script if env_script.exists() else None, n=n, budget=budget)
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
