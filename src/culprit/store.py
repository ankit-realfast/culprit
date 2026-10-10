"""Plain-file persistence under runs/: the triage queue and one folder per episode."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from culprit.taxonomy import Symptom
from culprit.triage import Candidate

CANDIDATES_FILE = "candidates.json"
SYMPTOM_FILE = "symptom.json"
RESTORE_FILE = "restore.json"  # how far the restored starting files can be trusted


class UnknownEpisode(ValueError):
    pass


class NotReady(FileNotFoundError):
    """A step that must come first has not been done; the message names the command that does it."""


def read_json(path: Path, default=None):
    """A JSON file under runs/, or default when it does not exist."""
    return json.loads(path.read_text()) if path.is_file() else default


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


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


def display_id(runs_dir: Path, episode_id: str) -> str:
    """The id to show in a suggested command: the short id when it leads back to this episode alone, else the full id."""
    shown = short_id(episode_id)
    try:
        return shown if resolve_episode_id(runs_dir, shown) == episode_id else episode_id
    except (UnknownEpisode, NotReady):
        return episode_id


def candidate_rows(runs_dir: Path) -> list[dict]:
    """The stored triage queue in row order (the latest ranking first); empty before the first triage."""
    return read_json(runs_dir / CANDIDATES_FILE, [])


def save_candidates(runs_dir: Path, candidates: list[Candidate]) -> Path:
    """Merge a new ranking into the stored queue by episode id, so a later triage never drops an episode
    already queued or confirmed. The new ranking comes first, in its own order, so the rows triage prints
    are queue rows #1..#n; kept episodes it no longer shows follow, highest score first."""
    fresh = [asdict(c) for c in candidates]
    fresh_ids = {c.summary.episode_id for c in candidates}
    kept = [row for row in candidate_rows(runs_dir) if row["summary"]["episode_id"] not in fresh_ids]
    return write_json(runs_dir / CANDIDATES_FILE, fresh + sorted(kept, key=lambda row: row["score"], reverse=True))


def record_symptom(runs_dir: Path, episode_id: str, symptom: Symptom) -> Path:
    if episode_id not in _candidate_ids(runs_dir):
        raise UnknownEpisode(f"{episode_id} is not in {runs_dir / CANDIDATES_FILE}")
    return write_json(episode_dir(runs_dir, episode_id) / SYMPTOM_FILE,
                      {"episode_id": episode_id, "symptom": symptom.value})


def confirm_step(runs_dir: Path, episode_id: str) -> str:
    """The command that records a symptom; confirm only accepts queued episodes, so name triage when needed."""
    shown = display_id(runs_dir, episode_id)
    if episode_id in {row["summary"]["episode_id"] for row in candidate_rows(runs_dir)}:
        return f"run `culprit confirm {shown} <symptom>`"
    return (f"it is not in the triage queue — run `culprit triage` until it is listed, "
            f"then `culprit confirm {shown} <symptom>`")


def read_symptom(runs_dir: Path, episode_id: str) -> Symptom | None:
    stored = read_json(episode_dir(runs_dir, episode_id) / SYMPTOM_FILE)
    return Symptom(stored["symptom"]) if stored else None


def load_restore(folder: Path) -> dict | None:
    return read_json(folder / RESTORE_FILE)


def _candidate_ids(runs_dir: Path) -> set[str]:
    path = runs_dir / CANDIDATES_FILE
    if not path.exists():
        raise NotReady(f"{path} does not exist — run `culprit triage` first")
    return {row["summary"]["episode_id"] for row in json.loads(path.read_text())}
