"""Reading Claude Code's session logs: episodes, failure signals, triage, locating one episode,
file-history snapshots and whether an episode can be replayed."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# --- episodes: Split a Claude Code session log into episodes. ---
# An episode is one human prompt and every record after it until the next human prompt. Episodes are
# cut by file order because only user records carry a promptId; assistant and system records do not.

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


# --- signals: The failure evidence one episode leaves in its log: a ranking input, never a verdict. ---

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


# --- triage: Rank episodes by how likely they are to have failed, for a human to confirm. ---
# The score orders the human's review queue; it never labels an episode a failure on its own.

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


# --- locate: Find one episode by its id, and the prompts the human typed after it. ---

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


# --- snapshot: The file-history snapshot Claude Code took when the episode's prompt arrived. ---
# It maps every file Claude Code had edited so far in the session to the backup holding that file's
# content at this point, or to None for a file that did not exist yet (an assumption Stage 2's
# restore check verifies on real episodes).

Tracked = dict[str, str | None]


def load_snapshot(episode: Episode) -> Tracked | None:
    if not episode.prompt_uuid:
        return None
    tracked: Tracked | None = None
    for record in read_records(episode.session_file):
        if record.get("type") != "file-history-snapshot":
            continue
        snap = record.get("snapshot") or {}
        if snap.get("messageId") == episode.prompt_uuid:
            # updates repeat the snapshot as the episode edits more files; the last one is complete
            tracked = {path: (entry or {}).get("backupFileName")
                       for path, entry in (snap.get("trackedFileBackups") or {}).items()}
    return tracked


def session_root(session_file: Path) -> Path | None:
    """The directory the session started in: file-history stores its relative paths against it,
    not against the working directory of a later episode that changed directory."""
    for record in read_records(session_file):
        if record.get("cwd"):
            return Path(record["cwd"])
    return None


# --- eligibility ---
# Why an episode cannot be replayed in v1 (an empty list means it can), and which project hooks a replay runs.

PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
ASK_USER_TOOL = "AskUserQuestion"


def replay_blockers(episode: Episode, tracked: Tracked | None) -> list[str]:
    blockers = []
    if not episode.cwd:
        blockers.append("no working directory was recorded")
    elif not Path(episode.cwd).is_dir():
        blockers.append(f"working directory {episode.cwd} no longer exists")
    elif Path(episode.cwd).resolve() == Path.home().resolve():
        blockers.append("working directory is the home directory; copying it would copy ~/.claude and ~/.ssh")
    elif Path(__file__).resolve().is_relative_to(Path(episode.cwd).resolve()):
        blockers.append("working directory contains culprit itself; a copy would copy culprit's own runs")
    elif not os.access(Path(episode.cwd).resolve().parent, os.W_OK):
        blockers.append("the working directory's parent is not writable, so no replay copy can sit beside it")
    elif (Path(episode.cwd) / ".git").is_file():
        blockers.append("working directory is a git worktree or submodule: its .git file points at another "
                        "repository, so a copy would still write into that repository")
    if tracked is None:
        blockers.append("no file-history snapshot exists for this prompt")
    names = {use.get("name", "") for use in tool_uses(episode)}
    if any(not (image.get("source") or {}).get("data") for image in prompt_images(episode)):
        blockers.append("the prompt carried an image whose data the log did not keep, so a replay cannot resend it")
    if ASK_USER_TOOL in names and not recorded_answers(episode):
        blockers.append("asked the human a question whose answer the log did not keep, so a replay cannot answer it")
    return blockers


def project_hooks(cwd: Path) -> list[str]:
    """Hook commands in the project's own settings; they run in every replay, so the human must see them."""
    commands = []
    for name in PROJECT_SETTINGS:
        path = cwd / name
        if not path.is_file():
            continue
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError:
            commands.append(f"{name} is not valid JSON")
            continue
        for event, matchers in (settings.get("hooks") or {}).items():
            for matcher in matchers or []:
                for hook in (matcher or {}).get("hooks") or []:
                    commands.append(f"{event}: {hook.get('command', '')}")
    return commands


def recorded_answers(episode: Episode) -> dict[str, str]:
    answers: dict[str, str] = {}
    for record in episode.records:
        result = record.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("answers"), dict):
            answers.update({str(q): str(a) for q, a in result["answers"].items()})
    return answers
