"""The failure evidence one episode leaves in its log: a ranking input, never a verdict."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import re

from culprit.episodes import Episode, content_blocks, excerpt, is_interrupt

CORRECTION_EXCERPT = 40
# Conservative on purpose: a phrase belongs here only if it nearly always means "you got that wrong".
# A false positive pushes a good episode to the top of the queue; a miss still leaves the other signals.
CORRECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    # "no" as an answer, not as a determiner: "no, ..." or "no i meant ..." but not "no tests needed".
    r"^(no|nope)(\s*([,.!;:]|—|-|$)|\s+(i|i'm|im|that|that's|this|it|it's|you|we|not|don't|dont|wait)\b)",
    r"^wrong\b",
    r"^(undo|revert|stop)\b",
    r"\b(undo|revert) (that|this|it|those|these|the|your)\b",
    r"\bstill (broken|failing|fails|wrong|not working|the same|(doesn't|does not|isn't|is not|won't|can't)\b)",
    r"\b(that|this|it)('s| is) (wrong|incorrect|not right|not what)\b",
    r"\b(it|this|that) (doesn't|does not|didn't|did not) work\b",
    r"\bnot what i (asked|wanted|meant|said)\b",
    r"\byou (didn't|did not|forgot|missed|ignored|broke)\b",
    r"\bwhy (are|did|were|would) (you|we)\b",
    r"\bi (told you|already said|said not)\b",
    r"\bsame (error|issue|problem|bug)\b",
)]


@dataclass(frozen=True)
class FailureSignals:
    interrupted: bool
    tool_errors: int
    api_errors: int
    compactions: int
    duration_s: float
    corrected: str = ""  # an excerpt of the human's next prompt when it corrects Claude, else empty


def is_correction(follow_up: str) -> bool:
    text = " ".join(follow_up.replace("\u2019", "'").split())[:300]
    return any(pattern.search(text) for pattern in CORRECTION_PATTERNS)


def signals_for(episode: Episode, follow_up: str | None = None) -> FailureSignals:
    """follow_up is the human's next prompt in the session, if there is one."""
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
        corrected=excerpt(follow_up, CORRECTION_EXCERPT) if follow_up and is_correction(follow_up) else "",
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
