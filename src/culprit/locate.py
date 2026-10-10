"""Find one episode by its id, and the prompts the human typed after it."""

from __future__ import annotations

from pathlib import Path

from culprit.episodes import Episode, episodes_from_session


class EpisodeNotFound(LookupError):
    pass


def find_session_file(projects_dir: Path, session_id: str) -> Path:
    matches = sorted(projects_dir.glob(f"*/{session_id}.jsonl"))
    if not matches:
        raise EpisodeNotFound(f"no session log named {session_id}.jsonl under {projects_dir}")
    return matches[0]


def find_episode(projects_dir: Path, episode_id: str) -> Episode:
    session_id, _, prompt_id = episode_id.partition(":")
    for episode in episodes_from_session(find_session_file(projects_dir, session_id)):
        if episode.prompt_id == prompt_id:
            return episode
    raise EpisodeNotFound(f"{episode_id} is not in its session log")


def follow_up_prompts(projects_dir: Path, episode_id: str, count: int = 3) -> list[str]:
    session_id, _, prompt_id = episode_id.partition(":")
    found = False
    follow_ups: list[str] = []
    for episode in episodes_from_session(find_session_file(projects_dir, session_id)):
        if found:
            follow_ups.append(episode.prompt_text)
            if len(follow_ups) == count:
                break
        elif episode.prompt_id == prompt_id:
            found = True
    if not found:
        raise EpisodeNotFound(f"{episode_id} is not in its session log")
    return follow_ups
