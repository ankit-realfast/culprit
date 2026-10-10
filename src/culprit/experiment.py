"""One variant of an episode, replayed N times and judged, inside a spend cap.

The outcome — passes out of runs — is what the verdict compares across variants.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.judge import JUDGE_CALL_CAP_USD, Judgement, judge_replay
from culprit.replay import ReplaySettings, replay_workdir, run_replay
from culprit.store import episode_dir

REPLAYS_DIR = "replays"
PRISTINE_DIR = "pristine"
OUTCOME_FILE = "outcome.json"
JUDGEMENT_FILE = "judgement.json"
RESULT_FILE = "result.json"
CALLS_FILE = "calls.json"
SUPERSEDED_DIR = "superseded"  # runs judged against an earlier criterion, kept so their spend still counts
MAX_VOID_RETRIES = 5  # extra attempts per variant when runs fail for reasons outside the agent


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
    runs: int  # counted runs; void runs are retried and left out
    cost_usd: float
    void: int = 0


def run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path,
                criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay,
                out_dir: Path | None = None) -> VariantOutcome:
    episode_folder = episode_dir(runs_dir, episode.episode_id)
    folder = out_dir or episode_folder
    variant_dir = folder / REPLAYS_DIR / variant
    passes = counted = void = 0
    cost = 0.0
    attempt = 0
    while counted < n and attempt < n + MAX_VOID_RETRIES:
        run_dir = variant_dir / str(attempt)
        attempt += 1
        if (run_dir / RESULT_FILE).is_file() and (run_dir / JUDGEMENT_FILE).is_file():
            # paid for before a stop: reuse it rather than pay again
            cost += float(json.loads((run_dir / RESULT_FILE).read_text()).get("cost_usd") or 0)
            judgement = Judgement(**json.loads((run_dir / JUDGEMENT_FILE).read_text()))
        else:
            budget.ensure_room(settings.max_budget_usd + JUDGE_CALL_CAP_USD)
            result = replay(settings, episode_folder / PRISTINE_DIR, run_dir, replay_workdir(episode),
                            criterion.checks, original_cwd=Path(episode.cwd))
            budget.spend(result.cost_usd)  # charged before judging, so a judge failure cannot lose it
            cost += result.cost_usd
            judgement = judge(criterion, result)
            if judgement.by == "model":
                budget.spend(JUDGE_CALL_CAP_USD)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / JUDGEMENT_FILE).write_text(json.dumps(asdict(judgement), indent=2))
        if judgement.by == "void":
            void += 1
            continue
        counted += 1
        passes += int(judgement.passed)
    outcome = VariantOutcome(variant, passes, counted, round(cost, 4), void)
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / OUTCOME_FILE).write_text(json.dumps(asdict(outcome), indent=2))
    return outcome


def stored_spend(folder: Path) -> float:
    """Everything an episode (or selftest) folder has already spent: replays, model judgements, other calls."""
    total = 0.0
    run_dirs = [*(folder / REPLAYS_DIR).glob("*/*"), *(folder / SUPERSEDED_DIR).glob(f"*/{REPLAYS_DIR}/*/*")]
    for run_dir in run_dirs:
        if (run_dir / RESULT_FILE).is_file():
            total += float(json.loads((run_dir / RESULT_FILE).read_text()).get("cost_usd") or 0)
        if (run_dir / JUDGEMENT_FILE).is_file() and json.loads((run_dir / JUDGEMENT_FILE).read_text()).get("by") == "model":
            total += JUDGE_CALL_CAP_USD
    for calls in [folder / CALLS_FILE, *(folder / SUPERSEDED_DIR).glob(f"*/{CALLS_FILE}")]:
        if calls.is_file():
            total += float(json.loads(calls.read_text()).get("usd") or 0)
    return round(total, 4)


def record_call_spend(folder: Path, usd: float) -> None:
    """Charge a drafting or leak-check call to the folder, at its cap (ask_claude does not report its cost)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CALLS_FILE
    spent = float(json.loads(path.read_text()).get("usd") or 0) if path.is_file() else 0.0
    path.write_text(json.dumps({"usd": round(spent + usd, 4)}))


def load_outcome(folder: Path, variant: str) -> VariantOutcome | None:
    path = folder / REPLAYS_DIR / variant / OUTCOME_FILE
    if not path.exists():
        return None
    return VariantOutcome(**json.loads(path.read_text()))
