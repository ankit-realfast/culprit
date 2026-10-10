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


class NotReady(FileNotFoundError):
    """A step that must come first has not been done; the message names the command that does it."""


def episode_dir(runs_dir: Path, episode_id: str) -> Path:
    return runs_dir / episode_id.replace(":", "__")


def short_id(episode_id: str) -> str:
    """The first 8 characters of the session id and of the prompt id; `resolve_episode_id` expands it."""
    session_id, _, prompt_id = episode_id.partition(":")
    return f"{session_id[:8]}:{prompt_id[:8]}"


def known_episode_ids(runs_dir: Path) -> list[str]:
    """Ids in the triage queue (queue order), then ids that have only an episode folder."""
    ids = [row["summary"]["episode_id"] for row in candidate_rows(runs_dir)]
    if runs_dir.is_dir():
        folders = sorted(p.name.replace("__", ":", 1) for p in runs_dir.iterdir() if p.is_dir() and "__" in p.name)
        ids += [i for i in folders if i not in ids]
    return ids


def resolve_episode_id(runs_dir: Path, text: str) -> str:
    """Expand `#n` (row n of the queue), a unique id prefix, or a short id to a full episode id.

    A full id passes through unchanged even when nothing in runs/ knows it yet.
    """
    if text.startswith("#") and text[1:].isdigit():
        rows = candidate_rows(runs_dir)
        if not rows:
            raise NotReady(f"{runs_dir / CANDIDATES_FILE} does not exist — run `culprit triage` first")
        if not 1 <= int(text[1:]) <= len(rows):
            raise UnknownEpisode(f"{text} is not in the queue: it has {len(rows)} rows")
        return rows[int(text[1:]) - 1]["summary"]["episode_id"]
    known = known_episode_ids(runs_dir)
    if ":" in text:
        if text in known:
            return text
        session_part, _, prompt_part = text.partition(":")
        matches = [i for i in known
                   if i.partition(":")[0].startswith(session_part) and i.partition(":")[2].startswith(prompt_part)]
        if not matches:
            return text
    else:
        matches = [i for i in known if i.startswith(text)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UnknownEpisode(f"no episode in {runs_dir.resolve()} matches {text}")
    raise UnknownEpisode(f"{text} matches {len(matches)} episodes: {', '.join(matches)}")


def candidate_rows(runs_dir: Path) -> list[dict]:
    """The stored triage queue, highest score first; empty before the first triage."""
    path = runs_dir / CANDIDATES_FILE
    return json.loads(path.read_text()) if path.exists() else []


def save_candidates(runs_dir: Path, candidates: list[Candidate]) -> Path:
    """Merge a new ranking into the stored queue by episode id (the new score wins), so a later triage
    never drops an episode already queued or confirmed."""
    rows = {row["summary"]["episode_id"]: row for row in candidate_rows(runs_dir)}
    rows.update({c.summary.episode_id: asdict(c) for c in candidates})
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / CANDIDATES_FILE
    path.write_text(json.dumps(sorted(rows.values(), key=lambda row: row["score"], reverse=True), indent=2))
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
        raise NotReady(f"{path} does not exist — run `culprit triage` first")
    return {row["summary"]["episode_id"] for row in json.loads(path.read_text())}
