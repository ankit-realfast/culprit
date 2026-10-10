"""The failure evidence one episode leaves in its log: a ranking input, never a verdict."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from culprit.episodes import Episode, content_blocks, is_interrupt


@dataclass(frozen=True)
class FailureSignals:
    interrupted: bool
    tool_errors: int
    api_errors: int
    compactions: int
    duration_s: float


def signals_for(episode: Episode) -> FailureSignals:
    records = episode.records
    return FailureSignals(
        interrupted=any(is_interrupt(record) for record in records),
        tool_errors=sum(
            1
            for record in records
            if record.get("type") == "user"
            for block in content_blocks(record)
            if block.get("type") == "tool_result" and block.get("is_error")
        ),
        api_errors=_count_system(records, "api_error"),
        compactions=_count_system(records, "compact_boundary"),
        duration_s=_duration_s(records),
    )


def _duration_s(records: tuple[dict, ...]) -> float:
    """Claude Code writes turn_duration only for turns that finish; an interrupted turn has none, so
    fall back to the span from the prompt to the last timestamped record."""
    turns = [
        record.get("durationMs") or 0
        for record in records
        if record.get("type") == "system" and record.get("subtype") == "turn_duration"
    ]
    if turns:
        return sum(turns) / 1000
    times = [t for t in (_parse_time(record.get("timestamp")) for record in records) if t is not None]
    return (max(times) - min(times)).total_seconds() if len(times) >= 2 else 0.0


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count_system(records: tuple[dict, ...], subtype: str) -> int:
    return sum(1 for record in records if record.get("type") == "system" and record.get("subtype") == subtype)
