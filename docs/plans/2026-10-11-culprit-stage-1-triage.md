# culprit Stage 1 — Episodes and Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `culprit triage` splits every Claude Code session log into episodes, ranks the ones most likely to have failed, and `culprit confirm` records the human's symptom label for each — the confirmed failures every later stage replays.

**Architecture:** A stdlib-only Python package with one module per job: `episodes` (split a session `.jsonl` into episodes), `signals` (count failure evidence in one episode), `triage` (score and rank episodes), `taxonomy` (the four symptoms), `store` (plain-file persistence under `runs/`), `cli` (the two commands). Sessions stream one file at a time, and each episode is reduced to a small summary before the next file is read, so 623 MB of logs never sit in memory at once.

**Tech Stack:** Python ≥3.11 (3.14.7 installed), uv 0.12.5, pytest ≥8 (dev only). No runtime dependencies.

**Spec:** [`docs/specs/2026-10-11-culprit-design.md`](../specs/2026-10-11-culprit-design.md) — this plan implements its §6 build step 1 ("episodes + triage on the real logs, with human confirmation (no replays yet)"). Steps 2–6 get their own plans once this one ships.

## Global Constraints

- Python `>=3.11`; runtime uses the standard library only; `pytest>=8` is the only dev dependency.
- Local only: no network calls, no model calls in this stage.
- Real session content never enters the repo: tests use synthetic records; `runs/` (which holds prompt excerpts of client work) is gitignored.
- Episode unit: one human prompt and every record after it until the next human prompt, identified as `<sessionId>:<promptId>`.
- Symptoms, exactly these values in this order: `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful`, plus `no-failure` for a candidate the human rejects (the spec's Misjudged check).
- Subagent logs (`<project>/<session>/subagents/*.jsonl`) are excluded in this stage.

## Log facts this plan relies on (measured 2026-10-11 over the real logs)

- `promptId` appears only on `type: "user"` records — the human prompt and the tool results that answer it. Assistant and system records carry none, so episodes are cut by **file order**, not by grouping on `promptId`.
- A human prompt is a `user` record whose content is a string, or a list of `text`/`image` blocks, with no `tool_result` block and no `isMeta: true`.
- Claude Code injects `user` records that start with markup: `<task-notification>`, `<command-name>`, `<bash-input>`, `<bash-stdout>`, `<local-command-stdout>`, `<command-message>`. These are not human prompts. (Consequence, accepted: a bare slash command such as `/compact` does not open an episode.)
- An interrupt is a `user` record whose text contains `[Request interrupted by user`; it belongs to the episode it interrupted.
- Failure evidence: `tool_result` blocks with `is_error: true`; `system` records with `subtype` `api_error`, `compact_boundary`, and `turn_duration` (whose `durationMs` is the turn's length).

## Review Focus

1. **A session still being written ends in a half-written line** — triage must skip that line and keep going, not crash. Pinned in Task 1 (`test_a_partial_last_line_is_skipped`).
2. **An interrupt must not start a new episode** — otherwise every interrupted episode looks clean and its interrupt is lost. Pinned in Task 1 (`test_an_interrupt_stays_inside_the_episode_it_interrupted`) and Task 2 (`test_an_interrupt_is_counted`).
3. **Injected markup must not count as your prompt** — task notifications and slash-command echoes would otherwise split episodes in half. Pinned in Task 1 (`test_injected_markup_is_not_a_human_prompt`).
4. **`confirm` with a mistyped episode id or before any triage** — must refuse with a message and write nothing. Pinned in Task 4 (`test_recording_an_unknown_episode_is_refused_and_writes_nothing`, `test_recording_before_triage_names_the_missing_step`).
5. **Confirming the same episode twice** — the second label replaces the first; there is one symptom per episode. Pinned in Task 4 (`test_a_second_label_replaces_the_first`).

---

## File Structure

```
culprit/
├── pyproject.toml            # package metadata, `culprit` console script, pytest config
├── .gitignore                # runs/, caches
├── src/culprit/
│   ├── __init__.py
│   ├── episodes.py           # Episode; split one session log into episodes
│   ├── signals.py            # FailureSignals; count failure evidence in one episode
│   ├── triage.py             # EpisodeSummary, Candidate; score and rank
│   ├── taxonomy.py           # Symptom
│   ├── store.py              # runs/candidates.json and runs/<episode>/symptom.json
│   └── cli.py                # `culprit triage`, `culprit confirm`
└── tests/
    ├── records.py            # builders for synthetic log records
    ├── conftest.py           # write_session fixture
    ├── test_episodes.py
    ├── test_signals.py
    ├── test_triage.py
    ├── test_store.py
    └── test_cli.py
```

---

### Task 1: Split a session log into episodes

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/culprit/__init__.py`, `src/culprit/episodes.py`
- Test: `tests/records.py`, `tests/conftest.py`, `tests/test_episodes.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `INTERRUPT_MARKER: str = "[Request interrupted by user"`
  - `@dataclass(frozen=True) class Episode` with fields `session_id: str`, `prompt_id: str`, `prompt_uuid: str | None` (the prompt record's `uuid`, which keys its file-history snapshot), `session_file: Path`, `prompt_text: str`, `started_at: str`, `cwd: str | None`, `git_branch: str | None`, `permission_mode: str | None`, `model: str | None`, `records: tuple[dict, ...]`, and property `episode_id -> str` returning `f"{session_id}:{prompt_id}"`.
  - `content_blocks(record: dict) -> list[dict]`
  - `text_of(record: dict) -> str`
  - `is_interrupt(record: dict) -> bool`
  - `is_human_prompt(record: dict) -> bool`
  - `episodes_from_session(session_file: Path) -> Iterator[Episode]`
  - `session_files(projects_dir: Path) -> list[Path]`

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "culprit"
version = "0.1.0"
description = "Find what to change when a Claude Code episode fails"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
culprit = "culprit.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/culprit"]

[tool.pytest.ini_options]
pythonpath = ["src", "tests"]
testpaths = ["tests"]
```

`.gitignore`:

```
runs/
.DS_Store
.venv/
__pycache__/
.pytest_cache/
```

`src/culprit/__init__.py`:

```python
"""culprit — find what to change when a Claude Code episode fails."""
```

Run: `cd /Users/akp/Downloads/culprit && uv sync`
Expected: creates `.venv` and installs pytest; exits 0.

- [ ] **Step 2: Write the record builders and the session fixture**

`tests/records.py`:

```python
"""Builders for synthetic Claude Code log records. Tests never use real session content."""


def human(text, prompt_id, session_id="s1", **extra):
    record = {
        "type": "user",
        "sessionId": session_id,
        "promptId": prompt_id,
        "uuid": f"u-{prompt_id}",
        "timestamp": "2026-10-01T10:00:00Z",
        "cwd": "/repo",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
    }
    record.update(extra)
    return record


def assistant(model="claude-opus-5", text="ok"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}]},
    }


def tool_result(prompt_id, is_error=False):
    return {
        "type": "user",
        "promptId": prompt_id,
        "toolUseResult": {},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": is_error}],
        },
    }


def interrupt(prompt_id):
    return {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
    }


def system(subtype, **extra):
    record = {"type": "system", "subtype": subtype}
    record.update(extra)
    return record
```

`tests/conftest.py`:

```python
import json

import pytest


@pytest.fixture
def write_session(tmp_path):
    """Write records as a session log under tmp_path/projects/<project>/<name>; return its path."""

    def write(records, name="s1.jsonl", project="-repo", trailing=""):
        project_dir = tmp_path / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records) + trailing)
        return path

    return write
```

- [ ] **Step 3: Write the failing tests**

`tests/test_episodes.py`:

```python
from culprit.episodes import episodes_from_session, is_human_prompt, session_files
from records import assistant, human, interrupt, system, tool_result


def test_each_human_prompt_starts_an_episode_holding_everything_until_the_next(write_session):
    path = write_session([
        human("fix the bug", "p1"), assistant(), tool_result("p1"), system("turn_duration", durationMs=1000),
        human("now add a test", "p2"), assistant(),
    ])
    episodes = list(episodes_from_session(path))
    assert [e.prompt_text for e in episodes] == ["fix the bug", "now add a test"]
    assert len(episodes[0].records) == 4
    assert len(episodes[1].records) == 2


def test_an_interrupt_stays_inside_the_episode_it_interrupted(write_session):
    path = write_session([human("refactor x", "p1"), assistant(), interrupt("p1"), human("stop, do y", "p2")])
    episodes = list(episodes_from_session(path))
    assert [e.prompt_text for e in episodes] == ["refactor x", "stop, do y"]
    assert len(episodes[0].records) == 3


def test_injected_markup_is_not_a_human_prompt():
    for text in ["<task-notification>done</task-notification>", "<command-name>/clear</command-name>",
                 "<bash-input>ls</bash-input>", "  <local-command-stdout>x</local-command-stdout>"]:
        assert not is_human_prompt(human(text, "p"))


def test_tool_results_meta_records_and_empty_text_are_not_prompts():
    assert not is_human_prompt(tool_result("p"))
    assert not is_human_prompt(human("caveat text", "p", isMeta=True))
    assert not is_human_prompt(human("   ", "p"))
    assert not is_human_prompt(assistant())


def test_a_prompt_with_only_an_image_is_a_prompt():
    assert is_human_prompt(human([{"type": "image", "source": {}}], "p"))


def test_a_partial_last_line_is_skipped(write_session):
    path = write_session([human("fix the bug", "p1"), assistant()], trailing='{"type": "assist')
    episodes = list(episodes_from_session(path))
    assert len(episodes) == 1
    assert len(episodes[0].records) == 2


def test_a_session_with_no_human_prompt_has_no_episodes(write_session):
    path = write_session([system("turn_duration", durationMs=5), assistant()])
    assert list(episodes_from_session(path)) == []


def test_an_episode_records_its_id_place_mode_and_first_real_model(write_session):
    path = write_session([human("fix it", "p1", session_id="abc", permissionMode="auto"),
                          assistant(model="<synthetic>"), assistant(model="claude-opus-5")])
    (episode,) = episodes_from_session(path)
    assert episode.episode_id == "abc:p1"
    assert episode.prompt_uuid == "u-p1"
    assert (episode.cwd, episode.git_branch, episode.model) == ("/repo", "main", "claude-opus-5")
    assert episode.permission_mode == "auto"
    assert episode.session_file == path


def test_session_files_skip_subagent_logs(write_session, tmp_path):
    main_log = write_session([human("x", "p1")])
    subagent_dir = main_log.parent / "s1" / "subagents"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "agent-1.jsonl").write_text("{}\n")
    assert session_files(tmp_path / "projects") == [main_log]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_episodes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.episodes'`

- [ ] **Step 5: Write the implementation**

`src/culprit/episodes.py`:

```python
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_episodes.py -v`
Expected: 9 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src/culprit/__init__.py src/culprit/episodes.py tests/records.py tests/conftest.py tests/test_episodes.py
git commit -m "Split Claude Code session logs into episodes

An episode is one human prompt and everything until the next, cut by file
order because only user records carry a promptId. Injected markup,
tool results, meta records and interrupts never open an episode.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

### Task 2: Count the failure evidence in one episode

**Files:**
- Create: `src/culprit/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `Episode`, `content_blocks`, `is_interrupt` from `culprit.episodes`.
- Produces:
  - `@dataclass(frozen=True) class FailureSignals` with fields `interrupted: bool`, `tool_errors: int`, `api_errors: int`, `compactions: int`, `duration_s: float`.
  - `signals_for(episode: Episode) -> FailureSignals`

- [ ] **Step 1: Write the failing tests**

`tests/test_signals.py`:

```python
from culprit.episodes import episodes_from_session
from culprit.signals import FailureSignals, signals_for
from records import assistant, human, interrupt, system, tool_result


def only_episode(write_session, records):
    (episode,) = episodes_from_session(write_session(records))
    return episode


def test_a_clean_episode_has_no_signals(write_session):
    episode = only_episode(write_session, [human("x", "p1"), assistant(), tool_result("p1")])
    assert signals_for(episode) == FailureSignals(False, 0, 0, 0, 0.0)


def test_an_interrupt_is_counted(write_session):
    episode = only_episode(write_session, [human("x", "p1"), assistant(), interrupt("p1")])
    assert signals_for(episode).interrupted is True


def test_errors_compactions_and_duration_are_counted(write_session):
    episode = only_episode(write_session, [
        human("x", "p1"),
        tool_result("p1", is_error=True), tool_result("p1", is_error=True), tool_result("p1"),
        system("api_error"), system("compact_boundary"),
        system("turn_duration", durationMs=30_000), system("turn_duration", durationMs=12_500),
    ])
    assert signals_for(episode) == FailureSignals(False, 2, 1, 1, 42.5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_signals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.signals'`

- [ ] **Step 3: Write the implementation**

`src/culprit/signals.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/signals.py tests/test_signals.py
git commit -m "Count each episode's failure evidence: interrupts, tool and API errors, compactions, duration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

### Task 3: Score and rank failure candidates

**Files:**
- Create: `src/culprit/triage.py`
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `Episode` from `culprit.episodes`; `FailureSignals`, `signals_for` from `culprit.signals`.
- Produces:
  - `@dataclass(frozen=True) class EpisodeSummary` with fields `episode_id: str`, `session_file: str`, `prompt_excerpt: str`, `signals: FailureSignals`, and classmethod `from_episode(episode: Episode) -> EpisodeSummary`.
  - `@dataclass(frozen=True) class Candidate` with fields `summary: EpisodeSummary`, `score: int`.
  - `excerpt(text: str, limit: int = 80) -> str`
  - `percentile(values: list[float], fraction: float) -> float`
  - `score(signals: FailureSignals, slow_threshold_s: float) -> int`
  - `rank_candidates(summaries: list[EpisodeSummary], top: int) -> list[Candidate]`

The weights are a first ordering for the human to review, not a verdict: interrupt 5, each tool error 1 (capped at 5), each API error 1, each compaction 2, slower than the 90th-percentile episode 3. They are revisited once confirmations exist.

- [ ] **Step 1: Write the failing tests**

`tests/test_triage.py`:

```python
from culprit.signals import FailureSignals
from culprit.triage import EpisodeSummary, excerpt, percentile, rank_candidates, score

CLEAN = FailureSignals(False, 0, 0, 0, 10.0)


def summary(episode_id, signals):
    return EpisodeSummary(episode_id, "/s.jsonl", "prompt", signals)


def test_excerpt_is_one_line_and_bounded():
    assert excerpt("fix\n  the   bug") == "fix the bug"
    assert excerpt("x" * 100, limit=10) == "x" * 9 + "…"


def test_percentile_uses_nearest_rank_and_survives_no_data():
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 0.9) == 9.0
    assert percentile([], 0.9) == float("inf")


def test_score_weights_each_signal():
    assert score(CLEAN, slow_threshold_s=60) == 0
    assert score(FailureSignals(True, 0, 0, 0, 0.0), 60) == 5
    assert score(FailureSignals(False, 9, 0, 0, 0.0), 60) == 5  # tool errors capped
    assert score(FailureSignals(False, 0, 2, 1, 0.0), 60) == 4
    assert score(FailureSignals(False, 0, 0, 0, 61.0), 60) == 3


def test_rank_orders_by_score_drops_clean_episodes_and_keeps_top():
    summaries = [
        summary("clean", CLEAN),
        summary("interrupted", FailureSignals(True, 0, 0, 0, 10.0)),
        summary("errored", FailureSignals(False, 2, 0, 0, 10.0)),
        summary("both", FailureSignals(True, 2, 0, 0, 10.0)),
    ]
    ranked = rank_candidates(summaries, top=2)
    assert [c.summary.episode_id for c in ranked] == ["both", "interrupted"]
    assert [c.score for c in ranked] == [7, 5]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_triage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.triage'`

- [ ] **Step 3: Write the implementation**

`src/culprit/triage.py`:

```python
"""Rank episodes by how likely they are to have failed, for a human to confirm.

The score orders the human's review queue; it never labels an episode a failure on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from culprit.episodes import Episode
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


@dataclass(frozen=True)
class Candidate:
    summary: EpisodeSummary
    score: int


def excerpt(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def score(signals: FailureSignals, slow_threshold_s: float) -> int:
    return (
        (INTERRUPT_WEIGHT if signals.interrupted else 0)
        + min(signals.tool_errors, TOOL_ERROR_CAP)
        + signals.api_errors
        + COMPACTION_WEIGHT * signals.compactions
        + (SLOW_WEIGHT if signals.duration_s > slow_threshold_s else 0)
    )


def rank_candidates(summaries: list[EpisodeSummary], top: int) -> list[Candidate]:
    slow_threshold_s = percentile([s.signals.duration_s for s in summaries], SLOW_PERCENTILE)
    scored = [Candidate(s, score(s.signals, slow_threshold_s)) for s in summaries]
    flagged = [c for c in scored if c.score > 0]
    return sorted(flagged, key=lambda c: c.score, reverse=True)[:top]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 16 passed. (`sorted` is stable, so equal scores keep log order.)

- [ ] **Step 5: Commit**

```bash
git add src/culprit/triage.py tests/test_triage.py
git commit -m "Rank episodes by failure evidence into a review queue for the human

Weights are a first ordering, not a verdict: interrupt 5, tool errors up
to 5, API error 1, compaction 2, slower than the p90 episode 3.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

### Task 4: Record the human's symptom label

**Files:**
- Create: `src/culprit/taxonomy.py`, `src/culprit/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Candidate`, `EpisodeSummary` from `culprit.triage`.
- Produces:
  - `class Symptom(StrEnum)` with members `STOPPED_SHORT = "stopped-short"`, `WRONG_RESULT = "wrong-result"`, `IGNORED_INSTRUCTIONS = "ignored-instructions"`, `WASTEFUL = "wasteful"`, `NO_FAILURE = "no-failure"`.
  - `class UnknownEpisode(ValueError)`
  - `CANDIDATES_FILE = "candidates.json"`, `SYMPTOM_FILE = "symptom.json"`
  - `episode_dir(runs_dir: Path, episode_id: str) -> Path`
  - `save_candidates(runs_dir: Path, candidates: list[Candidate]) -> Path`
  - `record_symptom(runs_dir: Path, episode_id: str, symptom: Symptom) -> Path`
  - `read_symptom(runs_dir: Path, episode_id: str) -> Symptom | None`

Note: `StrEnum` needs Python 3.11, which `requires-python` already guarantees.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
import json

import pytest

from culprit.signals import FailureSignals
from culprit.store import UnknownEpisode, episode_dir, read_symptom, record_symptom, save_candidates
from culprit.taxonomy import Symptom
from culprit.triage import Candidate, EpisodeSummary


def saved(runs_dir, *episode_ids):
    candidates = [Candidate(EpisodeSummary(i, "/s.jsonl", "prompt", FailureSignals(True, 0, 0, 0, 1.0)), 5)
                  for i in episode_ids]
    return save_candidates(runs_dir, candidates)


def test_symptoms_are_the_spec_values_in_order():
    assert [s.value for s in Symptom] == [
        "stopped-short", "wrong-result", "ignored-instructions", "wasteful", "no-failure"]


def test_saved_candidates_are_readable_json(tmp_path):
    path = saved(tmp_path, "abc:p1")
    (row,) = json.loads(path.read_text())
    assert row["summary"]["episode_id"] == "abc:p1"
    assert row["summary"]["signals"]["interrupted"] is True
    assert row["score"] == 5


def test_a_recorded_symptom_reads_back_from_the_episode_folder(tmp_path):
    saved(tmp_path, "abc:p1")
    path = record_symptom(tmp_path, "abc:p1", Symptom.WRONG_RESULT)
    assert path.parent == episode_dir(tmp_path, "abc:p1")
    assert ":" not in path.parent.name
    assert read_symptom(tmp_path, "abc:p1") is Symptom.WRONG_RESULT


def test_a_second_label_replaces_the_first(tmp_path):
    saved(tmp_path, "abc:p1")
    record_symptom(tmp_path, "abc:p1", Symptom.WASTEFUL)
    record_symptom(tmp_path, "abc:p1", Symptom.NO_FAILURE)
    assert read_symptom(tmp_path, "abc:p1") is Symptom.NO_FAILURE


def test_recording_an_unknown_episode_is_refused_and_writes_nothing(tmp_path):
    saved(tmp_path, "abc:p1")
    with pytest.raises(UnknownEpisode, match="abc:p2"):
        record_symptom(tmp_path, "abc:p2", Symptom.WASTEFUL)
    assert not episode_dir(tmp_path, "abc:p2").exists()


def test_recording_before_triage_names_the_missing_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="culprit triage"):
        record_symptom(tmp_path, "abc:p1", Symptom.WASTEFUL)


def test_an_unlabelled_episode_reads_as_none(tmp_path):
    assert read_symptom(tmp_path, "abc:p1") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.store'`

- [ ] **Step 3: Write the implementation**

`src/culprit/taxonomy.py`:

```python
"""The symptom axis of the spec's taxonomy: what went wrong in an episode, worst first."""

from enum import StrEnum


class Symptom(StrEnum):
    STOPPED_SHORT = "stopped-short"
    WRONG_RESULT = "wrong-result"
    IGNORED_INSTRUCTIONS = "ignored-instructions"
    WASTEFUL = "wasteful"
    NO_FAILURE = "no-failure"  # the human rejected the candidate: the spec's Misjudged check
```

`src/culprit/store.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 23 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/taxonomy.py src/culprit/store.py tests/test_store.py
git commit -m "Record the human's symptom label per episode under runs/

One symptom.json per episode folder; a label is accepted only for an
episode triage queued, and a second label replaces the first.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

### Task 5: The `culprit triage` and `culprit confirm` commands, proven on the real logs

**Files:**
- Create: `src/culprit/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `episodes_from_session`, `session_files` (episodes); `EpisodeSummary`, `rank_candidates` (triage); `Symptom` (taxonomy); `save_candidates`, `record_symptom`, `UnknownEpisode` (store).
- Produces: `main(argv: list[str] | None = None) -> int`, installed as the `culprit` console script.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
from culprit.cli import main
from culprit.store import read_symptom
from records import assistant, human, interrupt


def test_triage_prints_and_saves_the_ranked_queue(write_session, tmp_path, capsys):
    write_session([human("clean prompt", "p1", session_id="s1"), assistant(),
                   human("went wrong", "p2", session_id="s1"), assistant(), interrupt("p2")])
    runs = tmp_path / "runs"
    exit_code = main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs), "--top", "5"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "s1:p2" in out and "went wrong" in out and "interrupted" in out
    assert "s1:p1" not in out
    assert "2 episodes scanned" in out
    assert (runs / "candidates.json").exists()


def test_confirm_records_a_symptom_for_a_queued_episode(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    assert main(["confirm", "s1:p2", "stopped-short", "--runs", str(runs)]) == 0
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_confirm_refuses_an_unknown_episode_with_exit_1(tmp_path, capsys):
    assert main(["confirm", "nope:p9", "wasteful", "--runs", str(tmp_path / "runs")]) == 1
    assert "culprit triage" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.cli'`

- [ ] **Step 3: Write the implementation**

`src/culprit/cli.py`:

```python
"""The culprit command line: `triage` builds the review queue, `confirm` records the human's label."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from culprit.episodes import episodes_from_session, session_files
from culprit.signals import FailureSignals
from culprit.store import UnknownEpisode, record_symptom, save_candidates
from culprit.taxonomy import Symptom
from culprit.triage import EpisodeSummary, rank_candidates

DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_RUNS = Path("runs")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "triage":
        return _triage(args.projects, args.runs, args.top)
    return _confirm(args.runs, args.episode_id, Symptom(args.symptom))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", description="Find what to change when a Claude Code episode fails.")
    commands = parser.add_subparsers(dest="command", required=True)
    triage = commands.add_parser("triage", help="rank episodes most likely to have failed")
    triage.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    triage.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    triage.add_argument("--top", type=int, default=20)
    confirm = commands.add_parser("confirm", help="record what went wrong in a queued episode")
    confirm.add_argument("episode_id")
    confirm.add_argument("symptom", choices=[s.value for s in Symptom])
    confirm.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    return parser


def _triage(projects: Path, runs: Path, top: int) -> int:
    summaries = [
        EpisodeSummary.from_episode(episode)
        for session_file in session_files(projects)
        for episode in episodes_from_session(session_file)
    ]
    candidates = rank_candidates(summaries, top)
    path = save_candidates(runs, candidates)
    for candidate in candidates:
        s = candidate.summary
        print(f"{candidate.score:>3}  {s.episode_id}  [{_evidence(s.signals)}]  {s.prompt_excerpt}")
    print(f"{len(summaries)} episodes scanned; top {len(candidates)} saved to {path}")
    return 0


def _evidence(signals: FailureSignals) -> str:
    parts = []
    if signals.interrupted:
        parts.append("interrupted")
    if signals.tool_errors:
        parts.append(f"{signals.tool_errors} tool errors")
    if signals.api_errors:
        parts.append(f"{signals.api_errors} API errors")
    if signals.compactions:
        parts.append(f"{signals.compactions} compactions")
    parts.append(f"{signals.duration_s:.0f}s")
    return ", ".join(parts)


def _confirm(runs: Path, episode_id: str, symptom: Symptom) -> int:
    try:
        path = record_symptom(runs, episode_id, symptom)
    except (UnknownEpisode, FileNotFoundError) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return 1
    print(f"recorded {symptom.value} for {episode_id} in {path}")
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 26 passed.

- [ ] **Step 5: Prove it on the real logs**

Run: `cd /Users/akp/Downloads/culprit && time uv run culprit triage --top 20`
Expected:
- 20 ranked lines, each shaped like `  7  <session-uuid>:<prompt-uuid>  [interrupted, 2 tool errors, 140s]  <prompt excerpt>`
- a final line `N episodes scanned; top 20 saved to runs/candidates.json`, with N in the low thousands (the logs held 5,169 human prompts on 2026-10-11 before markup filtering, so N should be a little below that)
- exit 0. Note the wall time; it is the baseline for later stages.

Then confirm one real episode, picking the first id from that output:

Run: `uv run culprit confirm <first episode id> no-failure && cat runs/*/symptom.json`
Expected: `recorded no-failure for … in runs/…/symptom.json`, and the JSON shows that id and symptom.

Run: `git status --short`
Expected: `runs/` does not appear (it is gitignored); only `src/culprit/cli.py` and `tests/test_cli.py` are new.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/cli.py tests/test_cli.py
git commit -m "Add culprit triage and culprit confirm

triage streams every main session log, ranks failure candidates and saves
the queue to runs/candidates.json; confirm records the human's symptom.
Proven on the real logs: <N> episodes scanned in <wall time>.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

(Replace `<N>` and `<wall time>` with the numbers Step 5 printed.)
