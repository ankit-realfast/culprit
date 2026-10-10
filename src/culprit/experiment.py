"""One variant of an episode, replayed N times and judged, inside a spend cap.

The outcome — passes out of runs — is what the verdict compares across variants.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from culprit.model import CALL_CAP_USD, Criterion, Judgement, judge_replay
from culprit.logs import Episode
from culprit.replay import MEMORY_DIR, RESULT_FILE, ReplaySettings, replay_workdir, run_replay
from culprit.store import episode_dir, read_json, write_json

REPLAYS_DIR = "replays"
PRISTINE_DIR = "pristine"
OUTCOME_FILE = "outcome.json"
JUDGEMENT_FILE = "judgement.json"
CALLS_FILE = "calls.json"
SUPERSEDED_DIR = "superseded"  # runs judged against an earlier criterion, kept so their spend still counts
MAX_VOID_RETRIES = 5  # extra attempts per variant when runs fail for reasons outside the agent


def pristine_dir(runs_dir: Path, episode_id: str) -> Path:
    """The episode's restored starting files, which every replay (selftests included) is cloned from."""
    return episode_dir(runs_dir, episode_id) / PRISTINE_DIR


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


@dataclass(frozen=True)
class ReplayEvent:
    """What run_variant tells its progress callback: "start" before a replay, "finish" once it is judged."""
    kind: str  # "start" or "finish"
    variant: str
    attempt: int  # the run folder's number; void runs are retried under the next one
    index: int  # which of the n counted runs this attempt is for, from 1
    n: int
    result: str | None = None  # finish: "pass", "fail" or "void"
    cost_usd: float = 0.0  # finish: what this replay cost
    spent_usd: float = 0.0  # the episode's spend so far, this run and its judgement included
    reused: bool = False  # finish: paid for before a stop and read back from disk
    scope: str = ""  # who is running it when that is not obvious: a planted fault, an episode's short id


Progress = Callable[[ReplayEvent], None]


def scoped(progress: Progress | None, scope: str) -> Progress | None:
    """The same callback, with every event marked as belonging to `scope`."""
    if progress is None:
        return None
    return lambda event: progress(replace(event, scope=f"{scope} / {event.scope}" if event.scope else scope))


def run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path,
                criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay,
                out_dir: Path | None = None, progress: Progress | None = None) -> VariantOutcome:
    episode_folder = episode_dir(runs_dir, episode.episode_id)
    folder = out_dir or episode_folder
    variant_dir = folder / REPLAYS_DIR / variant
    passes = counted = void = 0
    cost = 0.0
    attempt = 0
    tell = progress or (lambda event: None)
    while counted < n and attempt < n + MAX_VOID_RETRIES:
        run_dir = variant_dir / str(attempt)
        event = ReplayEvent("start", variant, attempt, counted + 1, n, spent_usd=round(budget.spent_usd, 4))
        attempt += 1
        reused = (run_dir / RESULT_FILE).is_file() and (run_dir / JUDGEMENT_FILE).is_file()
        if reused:
            # paid for before a stop: reuse it rather than pay again
            run_cost = float(read_json(run_dir / RESULT_FILE).get("cost_usd") or 0)
            judgement = Judgement(**read_json(run_dir / JUDGEMENT_FILE))
        else:
            budget.ensure_room(settings.max_budget_usd + CALL_CAP_USD)
            tell(event)
            result = replay(settings, pristine_dir(runs_dir, episode.episode_id), run_dir, replay_workdir(episode),
                            criterion.checks, original_cwd=Path(episode.cwd),
                            original_memory=episode.session_file.parent / MEMORY_DIR)
            budget.spend(result.cost_usd)  # charged before judging, so a judge failure cannot lose it
            run_cost = result.cost_usd
            judgement = judge(criterion, result)
            if judgement.by == "model":
                budget.spend(CALL_CAP_USD)
            write_json(run_dir / JUDGEMENT_FILE, asdict(judgement))
        cost += run_cost
        verdict = "void" if judgement.by == "void" else "pass" if judgement.passed else "fail"
        tell(replace(event, kind="finish", result=verdict, cost_usd=run_cost, spent_usd=round(budget.spent_usd, 4),
                     reused=reused))
        if judgement.by == "void":
            void += 1
            continue
        counted += 1
        passes += int(judgement.passed)
    outcome = VariantOutcome(variant, passes, counted, round(cost, 4), void)
    write_json(variant_dir / OUTCOME_FILE, asdict(outcome))
    return outcome


def stored_spend(folder: Path) -> float:
    """Everything an episode (or selftest) folder has already spent: replays, model judgements, other calls."""
    total = 0.0
    run_dirs = [*(folder / REPLAYS_DIR).glob("*/*"), *(folder / SUPERSEDED_DIR).glob(f"*/{REPLAYS_DIR}/*/*")]
    for run_dir in run_dirs:
        total += float(read_json(run_dir / RESULT_FILE, {}).get("cost_usd") or 0)
        if read_json(run_dir / JUDGEMENT_FILE, {}).get("by") == "model":
            total += CALL_CAP_USD
    for calls in [folder / CALLS_FILE, *(folder / SUPERSEDED_DIR).glob(f"*/{CALLS_FILE}")]:
        total += float(read_json(calls, {}).get("usd") or 0)
    return round(total, 4)


def record_call_spend(folder: Path, usd: float) -> None:
    """Charge a drafting or leak-check call to the folder, at its cap (ask_claude does not report its cost)."""
    spent = float(read_json(folder / CALLS_FILE, {}).get("usd") or 0)
    write_json(folder / CALLS_FILE, {"usd": round(spent + usd, 4)})


def load_outcome(folder: Path, variant: str) -> VariantOutcome | None:
    data = read_json(folder / REPLAYS_DIR / variant / OUTCOME_FILE)
    return None if data is None else VariantOutcome(**data)


def load_outcomes(folder: Path) -> tuple[VariantOutcome, ...]:
    """Every variant measured in an episode (or selftest) folder, by variant name."""
    return tuple(VariantOutcome(**read_json(path)) for path in sorted((folder / REPLAYS_DIR).glob(f"*/{OUTCOME_FILE}")))
