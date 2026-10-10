"""Split a Claude Code session log into episodes.

An episode is one human prompt and every record after it until the next human prompt. Episodes are
cut by file order because only user records carry a promptId; assistant and system records do not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

INTERRUPT_MARKER = "[Request interrupted by user"
# where Claude Code keeps one folder of session logs per project
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class Episode:
    """One human prompt and everything Claude Code did until the next one."""

    session_id: str
    prompt_id: str
    prompt_uuid: str | None  # keys the episode's file-history snapshot
    session_file: Path
    prompt_text: str
    started_at: str
    cwd: str | None
    git_branch: str | None
    permission_mode: str | None
    model: str | None
    records: tuple[dict, ...]

    @property
    def episode_id(self) -> str:
        return f"{self.session_id}:{self.prompt_id}"


def read_records(session_file: Path) -> Iterator[dict]:
    with session_file.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a session still being written ends in a partial line
            if isinstance(record, dict):
                yield record


def content_blocks(record: dict) -> list[dict]:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def text_of(record: dict) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content_blocks(record) if block.get("type") == "text")


def excerpt(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def is_interrupt(record: dict) -> bool:
    return record.get("type") == "user" and INTERRUPT_MARKER in text_of(record)


def is_human_prompt(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    blocks = content_blocks(record)
    if any(block.get("type") == "tool_result" for block in blocks):
        return False
    text = text_of(record).strip()
    if INTERRUPT_MARKER in text:
        return False
    if text.startswith("<"):
        return False  # markup Claude Code injects: task notifications, slash commands, bash echoes
    return bool(text) or any(block.get("type") == "image" for block in blocks)


def episodes_from_session(session_file: Path) -> Iterator[Episode]:
    prompt: dict | None = None
    records: list[dict] = []
    for record in read_records(session_file):
        if is_human_prompt(record):
            if prompt is not None:
                yield _episode(session_file, prompt, records)
            prompt, records = record, [record]
        elif prompt is not None:
            records.append(record)
    if prompt is not None:
        yield _episode(session_file, prompt, records)


def session_files(projects_dir: Path) -> list[Path]:
    """Main session logs only; subagent logs live one level deeper and are excluded."""
    return sorted(projects_dir.glob("*/*.jsonl"))


def _first_real_model(records: list[dict]) -> str | None:
    for record in records:
        model = (record.get("message") or {}).get("model") if record.get("type") == "assistant" else None
        if model and model != SYNTHETIC_MODEL:
            return model
    return None


def _episode(session_file: Path, prompt: dict, records: list[dict]) -> Episode:
    return Episode(
        session_id=prompt.get("sessionId") or session_file.stem,
        prompt_id=prompt.get("promptId") or prompt.get("uuid") or "",
        prompt_uuid=prompt.get("uuid"),
        session_file=session_file,
        prompt_text=text_of(prompt),
        started_at=prompt.get("timestamp", ""),
        cwd=prompt.get("cwd"),
        git_branch=prompt.get("gitBranch"),
        permission_mode=prompt.get("permissionMode"),
        model=_first_real_model(records),
        records=tuple(records),
    )


def prompt_images(episode: Episode) -> list[dict]:
    """The image blocks the human attached to the prompt."""
    blocks = content_blocks(episode.records[0]) if episode.records else []
    return [block for block in blocks if block.get("type") == "image"]


def tool_uses(episode: Episode) -> list[dict]:
    """Every tool call the agent made in this episode, in order; each has a name and an input."""
    return [
        block
        for record in episode.records
        if record.get("type") == "assistant"
        for block in content_blocks(record)
        if block.get("type") == "tool_use"
    ]
