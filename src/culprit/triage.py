"""Rank episodes by how likely they are to have failed, for a human to confirm.

The score orders the human's review queue; it never labels an episode a failure on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from culprit.episodes import Episode, episodes_from_session, excerpt
from culprit.signals import FailureSignals, signals_for

INTERRUPT_WEIGHT = 5
TOOL_ERROR_CAP = 5
COMPACTION_WEIGHT = 2
SLOW_WEIGHT = 3
SLOW_PERCENTILE = 0.9


@dataclass(frozen=True)
class EpisodeSummary:
    """What triage keeps of an episode once its records are released."""

    episode_id: str
    session_file: str
    prompt_excerpt: str
    signals: FailureSignals

    @classmethod
    def from_episode(cls, episode: Episode) -> EpisodeSummary:
        return cls(episode.episode_id, str(episode.session_file), excerpt(episode.prompt_text), signals_for(episode))


def summarize_session(session_file: Path) -> list[EpisodeSummary]:
    """Summarize each episode in a session, holding one episode's records at a time."""
    return [EpisodeSummary.from_episode(episode) for episode in episodes_from_session(session_file)]


@dataclass(frozen=True)
class Candidate:
    summary: EpisodeSummary
    score: int


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def score(signals: FailureSignals, slow_threshold_s: float) -> int:
    """API errors are left out: they are infrastructure failures a replay would not reproduce."""
    return (
        (INTERRUPT_WEIGHT if signals.interrupted else 0)
        + min(signals.tool_errors, TOOL_ERROR_CAP)
        + COMPACTION_WEIGHT * signals.compactions
        + (SLOW_WEIGHT if signals.duration_s > slow_threshold_s else 0)
    )


def rank_candidates(summaries: list[EpisodeSummary], top: int) -> list[Candidate]:
    slow_threshold_s = percentile([s.signals.duration_s for s in summaries], SLOW_PERCENTILE)
    scored = [Candidate(s, score(s.signals, slow_threshold_s)) for s in summaries]
    flagged = [c for c in scored if c.score > 0]
    return sorted(flagged, key=lambda c: c.score, reverse=True)[:top]
