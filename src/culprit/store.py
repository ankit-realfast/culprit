"""Plain-file persistence under runs/: the triage queue and one folder per episode."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from culprit.taxonomy import Symptom
from culprit.triage import Candidate

CANDIDATES_FILE = "candidates.json"
SYMPTOM_FILE = "symptom.json"


class UnknownEpisode(ValueError):
    pass


def episode_dir(runs_dir: Path, episode_id: str) -> Path:
    return runs_dir / episode_id.replace(":", "__")


def save_candidates(runs_dir: Path, candidates: list[Candidate]) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / CANDIDATES_FILE
    path.write_text(json.dumps([asdict(c) for c in candidates], indent=2))
    return path


def record_symptom(runs_dir: Path, episode_id: str, symptom: Symptom) -> Path:
    if episode_id not in _candidate_ids(runs_dir):
        raise UnknownEpisode(f"{episode_id} is not in {runs_dir / CANDIDATES_FILE}")
    folder = episode_dir(runs_dir, episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SYMPTOM_FILE
    path.write_text(json.dumps({"episode_id": episode_id, "symptom": symptom.value}, indent=2))
    return path


def read_symptom(runs_dir: Path, episode_id: str) -> Symptom | None:
    path = episode_dir(runs_dir, episode_id) / SYMPTOM_FILE
    if not path.exists():
        return None
    return Symptom(json.loads(path.read_text())["symptom"])


def _candidate_ids(runs_dir: Path) -> set[str]:
    path = runs_dir / CANDIDATES_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist — run `culprit triage` first")
    return {row["summary"]["episode_id"] for row in json.loads(path.read_text())}
