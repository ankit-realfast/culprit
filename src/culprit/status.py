"""Where each episode stands, read from the files in runs/, and the command that moves it on."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from culprit.criterion import Criterion, criterion_step, load_criterion
from culprit.experiment import OUTCOME_FILE, PRISTINE_DIR, REPLAYS_DIR, VariantOutcome, load_outcome, stored_spend
from culprit.report import RESTORE_FILE
from culprit.store import candidate_rows, confirm_step, display_id, episode_dir, known_episode_ids, read_symptom
from culprit.verdict import Verdict, load_verdict

STAGES = ("not queued", "queued", "confirmed", "criterion drafted", "criterion approved", "restored", "baselined",
          "diagnosed")


@dataclass(frozen=True)
class EpisodeStatus:
    episode_id: str
    stage: str  # one of STAGES
    next_step: str  # what to run next, in words, naming the command
    symptom: str | None = None
    criterion: Criterion | None = None
    restore: dict | None = None  # restore's trust numbers (restore.json)
    outcomes: tuple[VariantOutcome, ...] = ()  # every variant measured so far
    spent_usd: float = 0.0
    verdict: Verdict | None = None


def episode_status(runs_dir: Path, episode_id: str) -> EpisodeStatus:
    folder = episode_dir(runs_dir, episode_id)
    queued = episode_id in {row["summary"]["episode_id"] for row in candidate_rows(runs_dir)}
    symptom = read_symptom(runs_dir, episode_id)
    criterion = load_criterion(runs_dir, episode_id)
    verdict = load_verdict(folder)
    shown = display_id(runs_dir, episode_id)
    symptom_value = symptom.value if symptom else None
    diagnose = f"run `culprit diagnose {shown}`"  # it restores and runs the baseline before the oracles
    steps = [  # (stage reached once done, done?, what to run when it is not)
        ("confirmed", symptom is not None, lambda: confirm_step(runs_dir, episode_id)),
        ("criterion drafted", criterion is not None, lambda: criterion_step(criterion, shown, symptom_value)),
        ("criterion approved", bool(criterion and criterion.approved),
         lambda: criterion_step(criterion, shown, symptom_value)),
        ("restored", (folder / PRISTINE_DIR).is_dir(), lambda: diagnose),
        ("baselined", load_outcome(folder, "baseline") is not None, lambda: diagnose),
        ("diagnosed", verdict is not None, lambda: diagnose),
    ]
    stage, hint = ("queued" if queued else "not queued"), None
    if verdict is not None:  # a verdict can come without a baseline (no-failure), so it settles the stage alone
        stage = "diagnosed"
    else:
        for reached, done, step in steps:
            if not done:
                hint = step()
                break
            stage = reached
    restore = json.loads((folder / RESTORE_FILE).read_text()) if (folder / RESTORE_FILE).is_file() else None
    outcomes = tuple(VariantOutcome(**json.loads(path.read_text()))
                     for path in sorted((folder / REPLAYS_DIR).glob(f"*/{OUTCOME_FILE}")))
    return EpisodeStatus(episode_id, stage, hint or "run `culprit report`", symptom_value,
                         criterion, restore, outcomes, stored_spend(folder), verdict)


def next_step(runs_dir: Path, episode_id: str) -> str:
    return episode_status(runs_dir, episode_id).next_step


def queue_status(runs_dir: Path) -> list[EpisodeStatus]:
    """Every episode in the triage queue (queue order), then every other episode with a folder."""
    return [episode_status(runs_dir, episode_id) for episode_id in known_episode_ids(runs_dir)]
