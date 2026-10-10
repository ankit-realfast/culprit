"""One variant of an episode, replayed N times and judged, inside a spend cap.

The outcome — passes out of runs — is what the verdict compares across variants.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.judge import JUDGE_CALL_CAP_USD, judge_replay
from culprit.replay import ReplaySettings, replay_workdir, run_replay
from culprit.store import episode_dir

REPLAYS_DIR = "replays"
PRISTINE_DIR = "pristine"
OUTCOME_FILE = "outcome.json"
JUDGEMENT_FILE = "judgement.json"


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class EpisodeBudget:
    cap_usd: float
    spent_usd: float = 0.0

    def ensure_room(self, next_cap_usd: float) -> None:
        if self.spent_usd + next_cap_usd > self.cap_usd:
            raise BudgetExhausted(f"the next run could spend ${next_cap_usd:.2f}; ${self.spent_usd:.2f} of "
                                  f"${self.cap_usd:.2f} is already spent")

    def spend(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass(frozen=True)
class VariantOutcome:
    variant: str
    passes: int
    runs: int
    cost_usd: float


def run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path,
                criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay,
                out_dir: Path | None = None) -> VariantOutcome:
    episode_folder = episode_dir(runs_dir, episode.episode_id)
    folder = out_dir or episode_folder
    variant_dir = folder / REPLAYS_DIR / variant
    passes = 0
    cost = 0.0
    for i in range(n):
        budget.ensure_room(settings.max_budget_usd + JUDGE_CALL_CAP_USD)
        run_dir = variant_dir / str(i)
        result = replay(settings, episode_folder / PRISTINE_DIR, run_dir, replay_workdir(episode), criterion.checks,
                        original_cwd=Path(episode.cwd))
        judgement = judge(criterion, result)
        spent = result.cost_usd + (JUDGE_CALL_CAP_USD if judgement.by == "model" else 0.0)
        budget.spend(spent)
        cost += result.cost_usd
        (run_dir / JUDGEMENT_FILE).write_text(json.dumps(asdict(judgement), indent=2))
        passes += int(judgement.passed)
    outcome = VariantOutcome(variant, passes, n, round(cost, 4))
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / OUTCOME_FILE).write_text(json.dumps(asdict(outcome), indent=2))
    return outcome


def load_outcome(folder: Path, variant: str) -> VariantOutcome | None:
    path = folder / REPLAYS_DIR / variant / OUTCOME_FILE
    if not path.exists():
        return None
    return VariantOutcome(**json.loads(path.read_text()))
