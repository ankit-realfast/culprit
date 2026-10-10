"""The failure evidence one episode leaves in its log: a ranking input, never a verdict."""

from __future__ import annotations

from dataclasses import dataclass

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
        duration_s=sum(
            record.get("durationMs") or 0
            for record in records
            if record.get("type") == "system" and record.get("subtype") == "turn_duration"
        )
        / 1000,
    )


def _count_system(records: tuple[dict, ...], subtype: str) -> int:
    return sum(1 for record in records if record.get("type") == "system" and record.get("subtype") == subtype)
