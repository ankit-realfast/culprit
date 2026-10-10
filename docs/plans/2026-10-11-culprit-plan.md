# culprit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build culprit end to end: from Claude Code session logs to a verdict per failed episode (what to change: task, environment, agent setup, harness or model) and a ranked fix list, with culprit's own accuracy measured.

**Spec:** [`docs/specs/2026-10-11-culprit-design.md`](../specs/2026-10-11-culprit-design.md)

**How to read this file:** one master plan in six stages, executed in order. Each stage ends in working, tested software and is proven on the real logs before the next starts. Each stage lists its own Goal, Architecture, Global Constraints, measured facts, Review Focus, files and tasks; a stage's tasks may use any interface an earlier stage produced.

| Stage | Delivers |
|---|---|
| 1 | `culprit triage` / `confirm`: episodes, ranked failure candidates, human symptom labels |
| 2 | `culprit criterion` / `restore`: approved success criterion; the episode's starting files rebuilt and checked |
| 3 | `culprit baseline`: locked-down replays judged against the criterion; baseline pass rate |
| 4 | `culprit diagnose`: oracle variants, leak checks, Fisher-test verdict, pair test |
| 5 | `culprit review` / `label` / `calibration` / `selftest`: judge agreement and planted-fault accuracy |
| 6 | `culprit report` / `diagnose-all`: symptom × cause grid and ranked fix list across 10–20 episodes |

---

## Stage 1 — Episodes and triage


**Goal:** `culprit triage` splits every Claude Code session log into episodes, ranks the ones most likely to have failed, and `culprit confirm` records the human's symptom label for each — the confirmed failures every later stage replays.

**Architecture:** A stdlib-only Python package with one module per job: `episodes` (split a session `.jsonl` into episodes), `signals` (count failure evidence in one episode), `triage` (score and rank episodes), `taxonomy` (the four symptoms), `store` (plain-file persistence under `runs/`), `cli` (the two commands). Sessions stream one file at a time, and each episode is reduced to a small summary before the next file is read, so 623 MB of logs never sit in memory at once.

**Tech Stack:** Python ≥3.11 (3.14.7 installed), uv 0.12.5, pytest ≥8 (dev only). No runtime dependencies.

**Spec:** [`docs/specs/2026-10-11-culprit-design.md`](../specs/2026-10-11-culprit-design.md) — this plan implements its §6 build step 1 ("episodes + triage on the real logs, with human confirmation (no replays yet)"). Steps 2–6 are the stages below.

### Global Constraints

- Python `>=3.11`; runtime uses the standard library only; `pytest>=8` is the only dev dependency.
- Local only: no network calls, no model calls in this stage.
- Real session content never enters the repo: tests use synthetic records; `runs/` (which holds prompt excerpts of client work) is gitignored.
- Episode unit: one human prompt and every record after it until the next human prompt, identified as `<sessionId>:<promptId>`.
- Symptoms, exactly these values in this order: `stopped-short`, `wrong-result`, `ignored-instructions`, `wasteful`, plus `no-failure` for a candidate the human rejects (the spec's Misjudged check).
- Subagent logs (`<project>/<session>/subagents/*.jsonl`) are excluded in this stage.

### Log facts this plan relies on (measured 2026-10-11 over the real logs)

- `promptId` appears only on `type: "user"` records — the human prompt and the tool results that answer it. Assistant and system records carry none, so episodes are cut by **file order**, not by grouping on `promptId`.
- A human prompt is a `user` record whose content is a string, or a list of `text`/`image` blocks, with no `tool_result` block and no `isMeta: true`.
- Claude Code injects `user` records that start with markup: `<task-notification>`, `<command-name>`, `<bash-input>`, `<bash-stdout>`, `<local-command-stdout>`, `<command-message>`. These are not human prompts. (Consequence, accepted: a bare slash command such as `/compact` does not open an episode.)
- An interrupt is a `user` record whose text contains `[Request interrupted by user`; it belongs to the episode it interrupted.
- Failure evidence: `tool_result` blocks with `is_error: true`; `system` records with `subtype` `api_error`, `compact_boundary`, and `turn_duration` (whose `durationMs` is the turn's length).

### Review Focus

1. **A session still being written ends in a half-written line** — triage must skip that line and keep going, not crash. Pinned in Task 1.1 (`test_a_partial_last_line_is_skipped`).
2. **An interrupt must not start a new episode** — otherwise every interrupted episode looks clean and its interrupt is lost. Pinned in Task 1.1 (`test_an_interrupt_stays_inside_the_episode_it_interrupted`) and Task 1.2 (`test_an_interrupt_is_counted`).
3. **Injected markup must not count as your prompt** — task notifications and slash-command echoes would otherwise split episodes in half. Pinned in Task 1.1 (`test_injected_markup_is_not_a_human_prompt`).
4. **`confirm` with a mistyped episode id or before any triage** — must refuse with a message and write nothing. Pinned in Task 1.4 (`test_recording_an_unknown_episode_is_refused_and_writes_nothing`, `test_recording_before_triage_names_the_missing_step`).
5. **Confirming the same episode twice** — the second label replaces the first; there is one symptom per episode. Pinned in Task 1.4 (`test_a_second_label_replaces_the_first`).

---

### File Structure

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

#### Task 1.1: Split a session log into episodes

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

#### Task 1.2: Count the failure evidence in one episode

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

#### Task 1.3: Score and rank failure candidates

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

#### Task 1.4: Record the human's symptom label

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

#### Task 1.5: The `culprit triage` and `culprit confirm` commands, proven on the real logs

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

---

## Stage 2 — Success criterion and restore


**Goal:** For a confirmed failed episode, `culprit criterion draft|show|approve` produces the human-approved definition of success, and `culprit restore` rebuilds the episode's starting files in a throwaway folder and proves the restore against the episode's own record of those files.

**Architecture:** Five new single-job modules on top of Stage 1: `locate` (find one episode and the prompts after it), `claude` (one schema-checked headless Claude call, the only place culprit talks to a model), `criterion` (draft, store, approve), `snapshot` + `eligibility` (what can be replayed), `sandbox` (clone, overlay backups, check). The CLI moves to one handler per subcommand.

**Tech Stack:** Python ≥3.11 stdlib; `claude` CLI 2.1.296 on PATH; macOS `cp -c` (APFS clone) with a `shutil.copytree` fallback; pytest.

**Spec:** [`docs/specs/2026-10-11-culprit-design.md`](../specs/2026-10-11-culprit-design.md) — §6 build step 2. Depends on Stage 1 above being merged.

### Global Constraints

- Every model call goes through `culprit.claude.ask_claude`, which always passes `--tools ""`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, `--permission-prompts none`, `--no-session-persistence` and a `--max-budget-usd` cap.
- Drafting model: `claude-sonnet-5-5`.
- Restores never touch the original working directory: every write goes to the destination folder, and every tracked path is checked to resolve inside it.
- Every git remote is removed from a restored folder.
- Tests never call the real `claude` binary; they inject a fake. One manual step per task that needs it runs the real thing.
- `runs/` stays gitignored; no real session content enters the repo.

### Facts this plan relies on (measured 2026-10-11)

- `file-history-snapshot` records look like `{"type": "file-history-snapshot", "messageId": …, "isSnapshotUpdate": …, "snapshot": {"messageId": <prompt record uuid>, "timestamp": …, "trackedFileBackups": {<path>: {"backupFileName": <name or null>, "version": <int>, "backupTime": …}}}}`. Paths are relative to `cwd` (22,715) or absolute (12,523).
- Backups live at `~/.claude/file-history/<sessionId>/<backupFileName>`; session log files are named `<sessionId>.jsonl`.
- An `Edit` tool result (`toolUseResult`) carries `filePath` and `originalFile`, the whole file before that edit. A `Read` result carries `file.content`.
- `claude -p … --output-format json --json-schema <schema> --tools ""` returns `{"is_error": false, "subtype": "success", "structured_output": {…}, "total_cost_usd": …}` (live call, $0.0016).

### Review Focus

1. **A tracked path that climbs out of the working directory** (`../../etc/hosts`, or an absolute path elsewhere) must never be written — it is listed as outside and skipped. Pinned in Task 2.5 (`test_paths_outside_the_working_directory_are_never_written`).
2. **A missing backup file** must be reported, not crash the restore and not silently leave today's content looking restored. Pinned in Task 2.5 (`test_a_missing_backup_is_reported`).
3. **`claude` failing** (non-JSON output, `is_error: true`, or no `structured_output`) must raise one named error carrying the reason. Pinned in Task 2.2 (`test_a_failed_call_raises_with_the_reason`, `test_non_json_output_raises_with_stderr`).
4. **Approving before drafting** must refuse with the next step named. Pinned in Task 2.3 (`test_approving_without_a_draft_names_the_missing_step`).
5. **An episode id whose session log is not on disk** must say so, not return an empty result. Pinned in Task 2.1 (`test_an_unknown_session_is_reported`).

---

### File Structure

```
src/culprit/
├── episodes.py      # MODIFY: add tool_uses()
├── locate.py        # find one episode; the prompts that followed it
├── claude.py        # ask_claude(): one schema-checked headless call
├── criterion.py     # Criterion; draft / save / load / approve
├── snapshot.py      # load the file-history snapshot keyed by the episode's prompt
├── eligibility.py   # why an episode cannot be replayed
├── sandbox.py       # clone, strip remotes, overlay backups, check against the episode
└── cli.py           # MODIFY: one handler per subcommand; add criterion, restore
tests/
├── records.py       # MODIFY: add tool_use, edit_result, snapshot builders
├── fakes.py         # FakeAsk: records prompts, returns canned structured output
├── test_locate.py
├── test_claude.py
├── test_criterion.py
├── test_snapshot_and_eligibility.py
├── test_sandbox.py
└── test_cli_stage2.py
```

---

#### Task 2.1: Find one episode and the prompts that followed it

**Files:**
- Modify: `src/culprit/episodes.py` (append `tool_uses`), `tests/records.py` (append builders)
- Create: `src/culprit/locate.py`
- Test: `tests/test_locate.py`

**Interfaces:**
- Consumes: `Episode`, `episodes_from_session`, `content_blocks` from `culprit.episodes`.
- Produces:
  - `tool_uses(episode: Episode) -> list[dict]` in `culprit.episodes` — every `tool_use` block in the episode's assistant records, in order (each has `name` and `input`).
  - `class EpisodeNotFound(LookupError)`
  - `find_session_file(projects_dir: Path, session_id: str) -> Path`
  - `find_episode(projects_dir: Path, episode_id: str) -> Episode`
  - `follow_up_prompts(projects_dir: Path, episode_id: str, count: int = 3) -> list[str]`

- [ ] **Step 1: Add the builders**

Append to `tests/records.py`:

```python
def tool_use(name, **tool_input):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5",
                    "content": [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]},
    }


def edit_result(prompt_id, file_path, original_file):
    return {
        "type": "user",
        "promptId": prompt_id,
        "toolUseResult": {"filePath": file_path, "oldString": "a", "newString": "b",
                          "originalFile": original_file, "replaceAll": False},
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    }


def snapshot(message_id, tracked):
    """tracked maps a path to a backup file name, or to None for a file that did not exist yet."""
    return {
        "type": "file-history-snapshot",
        "messageId": message_id,
        "isSnapshotUpdate": False,
        "snapshot": {
            "messageId": message_id,
            "timestamp": "2026-10-01T10:00:00Z",
            "trackedFileBackups": {
                path: {"backupFileName": name, "version": 1, "backupTime": "2026-10-01T10:00:00Z"}
                for path, name in tracked.items()
            },
        },
    }
```

- [ ] **Step 2: Write the failing tests**

`tests/test_locate.py`:

```python
import pytest

from culprit.episodes import episodes_from_session, tool_uses
from culprit.locate import EpisodeNotFound, find_episode, follow_up_prompts
from records import assistant, human, tool_use


def test_tool_uses_lists_every_tool_call_in_order(write_session):
    path = write_session([human("x", "p1"), tool_use("Read", file_path="/repo/a.py"),
                          assistant(), tool_use("Edit", file_path="/repo/a.py")])
    (episode,) = episodes_from_session(path)
    assert [(t["name"], t["input"]["file_path"]) for t in tool_uses(episode)] == [
        ("Read", "/repo/a.py"), ("Edit", "/repo/a.py")]


def test_find_episode_returns_the_matching_episode(write_session, tmp_path):
    write_session([human("first", "p1"), human("second", "p2")], name="s1.jsonl")
    episode = find_episode(tmp_path / "projects", "s1:p2")
    assert episode.prompt_text == "second"


def test_an_unknown_session_is_reported(tmp_path):
    (tmp_path / "projects").mkdir()
    with pytest.raises(EpisodeNotFound, match="nope.jsonl"):
        find_episode(tmp_path / "projects", "nope:p1")


def test_an_unknown_prompt_in_a_known_session_is_reported(write_session, tmp_path):
    write_session([human("first", "p1")], name="s1.jsonl")
    with pytest.raises(EpisodeNotFound, match="s1:p9"):
        find_episode(tmp_path / "projects", "s1:p9")


def test_follow_ups_are_the_next_human_prompts_up_to_count(write_session, tmp_path):
    write_session([human("a", "p1"), human("b", "p2"), human("c", "p3"), human("d", "p4"), human("e", "p5")],
                  name="s1.jsonl")
    assert follow_up_prompts(tmp_path / "projects", "s1:p2", count=2) == ["c", "d"]
    assert follow_up_prompts(tmp_path / "projects", "s1:p5") == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_locate.py -v`
Expected: FAIL — `ImportError: cannot import name 'tool_uses' from 'culprit.episodes'`

- [ ] **Step 4: Write the implementation**

Append to `src/culprit/episodes.py`:

```python
def tool_uses(episode: Episode) -> list[dict]:
    """Every tool call the agent made in this episode, in order; each has a name and an input."""
    return [
        block
        for record in episode.records
        if record.get("type") == "assistant"
        for block in content_blocks(record)
        if block.get("type") == "tool_use"
    ]
```

`src/culprit/locate.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: all Stage 1 tests plus 5 new pass (31 passed).

- [ ] **Step 6: Commit**

```bash
git add src/culprit/episodes.py src/culprit/locate.py tests/records.py tests/test_locate.py
git commit -m "Find one episode by id and the prompts the human typed after it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 2.2: One schema-checked headless Claude call

**Files:**
- Create: `src/culprit/claude.py`, `tests/fakes.py`
- Test: `tests/test_claude.py`

**Interfaces:**
- Consumes: nothing from culprit.
- Produces:
  - `NO_MCP_SERVERS = '{"mcpServers":{}}'`
  - `DRAFTING_MODEL = "claude-sonnet-5-5"`
  - `class ClaudeCallFailed(RuntimeError)`
  - `structured_call_command(prompt: str, schema: dict, model: str, max_budget_usd: float) -> list[str]`
  - `CALLS_DIR = Path.home() / ".culprit" / "calls"` — the empty folder every call runs from.
  - `ask_claude(prompt: str, schema: dict, model: str = DRAFTING_MODEL, max_budget_usd: float = 0.50, run=subprocess.run, cwd: Path = CALLS_DIR) -> dict` — returns `structured_output`.
  - `Ask = Callable[[str, dict], dict]` — the type every caller accepts so tests can inject `FakeAsk`.
  - In `tests/fakes.py`: `class FakeAsk` — `FakeAsk(*responses)`; calling it appends `(prompt, schema)` to `.calls` and returns the next response.

- [ ] **Step 1: Write the fake and the failing tests**

`tests/fakes.py`:

```python
import subprocess


class FakeAsk:
    """Stands in for culprit.claude.ask_claude: records each prompt, returns canned answers in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, prompt, schema):
        self.calls.append((prompt, schema))
        return self.responses.pop(0)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)
```

`tests/test_claude.py`:

```python
import json

import pytest

from culprit.claude import ClaudeCallFailed, ask_claude, structured_call_command
from fakes import completed

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_the_command_is_locked_down():
    command = structured_call_command("judge this", SCHEMA, "claude-sonnet-5-5", 0.5)
    assert command[:3] == ["claude", "-p", "judge this"]
    joined = " ".join(command)
    for required in ["--output-format json", "--strict-mcp-config", '--mcp-config {"mcpServers":{}}',
                     "--permission-prompts none", "--no-session-persistence", "--max-budget-usd 0.50",
                     "--model claude-sonnet-5-5"]:
        assert required in joined
    assert command[command.index("--tools") + 1] == ""
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA


def test_structured_output_is_returned_from_a_call_run_in_its_own_folder(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return completed(json.dumps({"is_error": False, "subtype": "success", "structured_output": {"ok": True}}))

    assert ask_claude("q", SCHEMA, run=run, cwd=tmp_path / "calls") == {"ok": True}
    assert calls[0]["capture_output"] is True
    assert calls[0]["cwd"] == tmp_path / "calls"
    assert (tmp_path / "calls").is_dir()


def test_a_failed_call_raises_with_the_reason(tmp_path):
    def run(command, **kwargs):
        return completed(json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": "over budget"}))

    with pytest.raises(ClaudeCallFailed, match="over budget"):
        ask_claude("q", SCHEMA, run=run, cwd=tmp_path)


def test_non_json_output_raises_with_stderr(tmp_path):
    def run(command, **kwargs):
        return completed("", stderr="Error: --json-schema is not a valid JSON Schema", returncode=1)

    with pytest.raises(ClaudeCallFailed, match="not a valid JSON Schema"):
        ask_claude("q", SCHEMA, run=run, cwd=tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_claude.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.claude'`

- [ ] **Step 3: Write the implementation**

`src/culprit/claude.py`:

```python
"""The one place culprit asks a model a question: a locked-down headless Claude call with a JSON schema.

No tools, no MCP servers, no permission prompts, no session log, and a spend cap on every call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

NO_MCP_SERVERS = '{"mcpServers":{}}'
DRAFTING_MODEL = "claude-sonnet-5-5"
CALL_TIMEOUT_S = 600
# Calls run from their own empty folder, so they never read a project's CLAUDE.md and the empty
# memory folder Claude Code creates for them is easy to recognise under ~/.claude/projects.
CALLS_DIR = Path.home() / ".culprit" / "calls"

Ask = Callable[[str, dict], dict]


class ClaudeCallFailed(RuntimeError):
    pass


def structured_call_command(prompt: str, schema: dict, model: str, max_budget_usd: float) -> list[str]:
    return [
        "claude", "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", NO_MCP_SERVERS,
        "--permission-prompts", "none",
        "--no-session-persistence",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]


def ask_claude(prompt: str, schema: dict, model: str = DRAFTING_MODEL, max_budget_usd: float = 0.50,
               run=subprocess.run, cwd: Path = CALLS_DIR) -> dict:
    cwd.mkdir(parents=True, exist_ok=True)
    completed = run(structured_call_command(prompt, schema, model, max_budget_usd),
                    capture_output=True, text=True, timeout=CALL_TIMEOUT_S, cwd=cwd)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ClaudeCallFailed(f"claude exited {completed.returncode}: {completed.stderr.strip()[:500]}") from None
    if payload.get("is_error") or "structured_output" not in payload:
        reason = payload.get("result") or payload.get("subtype") or "no structured output"
        raise ClaudeCallFailed(f"claude returned no structured output: {reason}")
    return payload["structured_output"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 35 passed.

- [ ] **Step 5: Prove one real call**

Run:

```bash
uv run python -c "
from culprit.claude import ask_claude
print(ask_claude('Is 7 a prime number?', {'type':'object','properties':{'prime':{'type':'boolean'}},'required':['prime']}, model='claude-haiku-5-5', max_budget_usd=0.05))"
```

Expected: `{'prime': True}`. Then confirm the call left no session log: `find ~/.claude/projects/*-culprit-calls -name '*.jsonl' | wc -l` prints 0 (only an empty `memory/` folder exists there).

- [ ] **Step 6: Commit**

```bash
git add src/culprit/claude.py tests/fakes.py tests/test_claude.py
git commit -m "Ask Claude one schema-checked question through a locked-down headless call

No tools, no MCP servers, no permission prompts, no session log, spend cap.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 2.3: Draft, store and approve an episode's success criterion

**Files:**
- Create: `src/culprit/criterion.py`
- Test: `tests/test_criterion.py`

**Interfaces:**
- Consumes: `Episode` (episodes); `Ask`, `ask_claude` (claude); `episode_dir` (store).
- Produces:
  - `@dataclass(frozen=True) class Criterion` with `success_means: str`, `checks: tuple[str, ...]`, `must_not: tuple[str, ...]`, `approved: bool = False`.
  - `CRITERION_FILE = "criterion.json"`, `CRITERION_SCHEMA: dict`
  - `drafting_prompt(episode: Episode, follow_ups: list[str]) -> str`
  - `draft_criterion(episode: Episode, follow_ups: list[str], ask: Ask = ask_claude) -> Criterion`
  - `save_criterion(runs_dir: Path, episode_id: str, criterion: Criterion) -> Path`
  - `load_criterion(runs_dir: Path, episode_id: str) -> Criterion | None`
  - `approve_criterion(runs_dir: Path, episode_id: str) -> Criterion`

The human may edit `runs/<episode>/criterion.json` by hand between `draft` and `approve`; `approve` reads whatever is on disk.

- [ ] **Step 1: Write the failing tests**

`tests/test_criterion.py`:

```python
import json

import pytest

from culprit.criterion import (Criterion, approve_criterion, draft_criterion, drafting_prompt,
                               load_criterion, save_criterion)
from culprit.episodes import episodes_from_session
from fakes import FakeAsk
from records import human

DRAFT = {"success_means": "pytest passes and the login bug no longer reproduces",
         "checks": ["pytest -q"], "must_not": ["delete tests"]}


def an_episode(write_session):
    (episode,) = episodes_from_session(write_session([human("fix the login bug", "p1")]))
    return episode


def test_the_drafting_prompt_carries_the_request_and_the_reaction(write_session):
    prompt = drafting_prompt(an_episode(write_session), ["no, it still fails on empty passwords"])
    assert "fix the login bug" in prompt
    assert "still fails on empty passwords" in prompt
    assert "never the implementation" in prompt


def test_a_draft_is_unapproved_and_built_from_the_answer(write_session):
    ask = FakeAsk(DRAFT)
    criterion = draft_criterion(an_episode(write_session), ["it still fails"], ask=ask)
    assert criterion == Criterion(DRAFT["success_means"], ("pytest -q",), ("delete tests",), approved=False)
    assert ask.calls[0][1]["required"] == ["success_means", "checks", "must_not"]


def test_save_load_and_approve_round_trip(tmp_path):
    save_criterion(tmp_path, "s1:p1", Criterion("works", ("make test",), ()))
    assert load_criterion(tmp_path, "s1:p1").approved is False
    approved = approve_criterion(tmp_path, "s1:p1")
    assert approved.approved is True
    assert load_criterion(tmp_path, "s1:p1") == approved


def test_approve_reads_the_human_edited_file(tmp_path):
    path = save_criterion(tmp_path, "s1:p1", Criterion("works", (), ()))
    edited = json.loads(path.read_text())
    edited["success_means"] = "the CSV export has a header row"
    path.write_text(json.dumps(edited))
    assert approve_criterion(tmp_path, "s1:p1").success_means == "the CSV export has a header row"


def test_approving_without_a_draft_names_the_missing_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="culprit criterion draft"):
        approve_criterion(tmp_path, "s1:p1")


def test_an_undrafted_episode_loads_as_none(tmp_path):
    assert load_criterion(tmp_path, "s1:p1") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_criterion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.criterion'`

- [ ] **Step 3: Write the implementation**

`src/culprit/criterion.py`:

```python
"""What success means for one episode: drafted by a model from the human's reaction, approved by the human.

Every pass/fail judgement culprit makes about a replay is made against this criterion.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from culprit.claude import Ask, ask_claude
from culprit.episodes import Episode
from culprit.store import episode_dir

CRITERION_FILE = "criterion.json"
CRITERION_SCHEMA = {
    "type": "object",
    "properties": {
        "success_means": {"type": "string"},
        "checks": {"type": "array", "items": {"type": "string"}},
        "must_not": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["success_means", "checks", "must_not"],
}


@dataclass(frozen=True)
class Criterion:
    success_means: str
    checks: tuple[str, ...]
    must_not: tuple[str, ...]
    approved: bool = False


def drafting_prompt(episode: Episode, follow_ups: list[str]) -> str:
    reactions = "\n".join(f"<message>{text}</message>" for text in follow_ups) or "<message>(none)</message>"
    return f"""You are writing the success criterion for one request a person made to a coding agent.
A later step will re-run the agent on this request and judge each attempt against what you write.

The request:
<request>{episode.prompt_text}</request>

What the person typed next. Their reaction reveals what they actually wanted:
<follow_ups>
{reactions}
</follow_ups>

Working directory: {episode.cwd}

Write three things:
- success_means: one or two sentences stating the observable outcome a correct attempt produces.
- checks: shell commands, run in the working directory after the attempt, that exit 0 only when the
  attempt succeeded. Leave the list empty unless such a command can be written reliably.
- must_not: things a correct attempt must not do.
Describe the outcome, never the implementation: do not name the fix, the file to change, or the code."""


def draft_criterion(episode: Episode, follow_ups: list[str], ask: Ask = ask_claude) -> Criterion:
    answer = ask(drafting_prompt(episode, follow_ups), CRITERION_SCHEMA)
    return Criterion(answer["success_means"], tuple(answer["checks"]), tuple(answer["must_not"]), approved=False)


def save_criterion(runs_dir: Path, episode_id: str, criterion: Criterion) -> Path:
    folder = episode_dir(runs_dir, episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CRITERION_FILE
    path.write_text(json.dumps(asdict(criterion), indent=2))
    return path


def load_criterion(runs_dir: Path, episode_id: str) -> Criterion | None:
    path = episode_dir(runs_dir, episode_id) / CRITERION_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Criterion(data["success_means"], tuple(data["checks"]), tuple(data["must_not"]), bool(data["approved"]))


def approve_criterion(runs_dir: Path, episode_id: str) -> Criterion:
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None:
        raise FileNotFoundError(f"no criterion for {episode_id} — run `culprit criterion draft {episode_id}` first")
    approved = replace(criterion, approved=True)
    save_criterion(runs_dir, episode_id, approved)
    return approved
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 41 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/criterion.py tests/test_criterion.py
git commit -m "Draft an episode's success criterion from the human's reaction; approve it by hand

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 2.4: Load the episode's snapshot and decide whether it can be replayed

**Files:**
- Create: `src/culprit/snapshot.py`, `src/culprit/eligibility.py`
- Test: `tests/test_snapshot_and_eligibility.py`

**Interfaces:**
- Consumes: `Episode`, `read_records`, `tool_uses` (episodes).
- Produces:
  - `Tracked = dict[str, str | None]` — tracked path → backup file name, or `None` for a file that did not exist yet.
  - `load_snapshot(episode: Episode) -> Tracked | None` — the last `file-history-snapshot` whose `snapshot.messageId` equals `episode.prompt_uuid`.
  - `replay_blockers(episode: Episode, tracked: Tracked | None) -> list[str]` — empty means replayable.

- [ ] **Step 1: Write the failing tests**

`tests/test_snapshot_and_eligibility.py`:

```python
from culprit.eligibility import replay_blockers
from culprit.episodes import episodes_from_session
from culprit.snapshot import load_snapshot
from records import human, snapshot, tool_use


def only_episode(write_session, records):
    (episode,) = episodes_from_session(write_session(records))
    return episode


def test_the_snapshot_keyed_by_the_prompt_uuid_is_loaded_and_the_last_one_wins(write_session):
    episode = only_episode(write_session, [
        snapshot("u-p1", {"a.py": "abc@v1"}),
        human("x", "p1"),
        snapshot("u-other", {"z.py": "zzz@v1"}),
        snapshot("u-p1", {"a.py": "abc@v1", "new.py": None}),
    ])
    assert load_snapshot(episode) == {"a.py": "abc@v1", "new.py": None}


def test_no_matching_snapshot_loads_as_none(write_session):
    episode = only_episode(write_session, [human("x", "p1"), snapshot("u-other", {"a.py": "x@v1"})])
    assert load_snapshot(episode) is None


def test_a_replayable_episode_has_no_blockers(write_session, tmp_path):
    episode = only_episode(write_session, [human("x", "p1", cwd=str(tmp_path)), tool_use("Edit", file_path="a")])
    assert replay_blockers(episode, {"a": "x@v1"}) == []


def test_every_blocker_is_named(write_session, tmp_path):
    episode = only_episode(write_session, [
        human("x", "p1", cwd=str(tmp_path / "gone")),
        tool_use("mcp__slack__send_message", text="hi"),
        tool_use("AskUserQuestion", questions=[]),
    ])
    blockers = replay_blockers(episode, None)
    assert any("no longer exists" in b for b in blockers)
    assert any("snapshot" in b for b in blockers)
    assert any("MCP" in b for b in blockers)
    assert any("asked the human" in b for b in blockers)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_snapshot_and_eligibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.eligibility'`

- [ ] **Step 3: Write the implementation**

`src/culprit/snapshot.py`:

```python
"""The file-history snapshot Claude Code took when the episode's prompt arrived.

It maps every file Claude Code had edited so far in the session to the backup holding that file's
content at this point, or to None for a file that did not exist yet (an assumption Stage 2's
restore check verifies on real episodes).
"""

from __future__ import annotations

from culprit.episodes import Episode, read_records

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
```

`src/culprit/eligibility.py`:

```python
"""Why an episode cannot be replayed in v1. An empty list means it can."""

from __future__ import annotations

from pathlib import Path

from culprit.episodes import Episode, tool_uses
from culprit.snapshot import Tracked

ASK_USER_TOOL = "AskUserQuestion"
MCP_TOOL_PREFIX = "mcp__"


def replay_blockers(episode: Episode, tracked: Tracked | None) -> list[str]:
    blockers = []
    if not episode.cwd:
        blockers.append("no working directory was recorded")
    elif not Path(episode.cwd).is_dir():
        blockers.append(f"working directory {episode.cwd} no longer exists")
    if tracked is None:
        blockers.append("no file-history snapshot exists for this prompt")
    names = {use.get("name", "") for use in tool_uses(episode)}
    if any(name.startswith(MCP_TOOL_PREFIX) for name in names):
        blockers.append("used an MCP tool, and v1 replays connect no MCP server")
    if ASK_USER_TOOL in names:
        blockers.append("asked the human a question, which a replay cannot answer")
    return blockers
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 45 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/snapshot.py src/culprit/eligibility.py tests/test_snapshot_and_eligibility.py
git commit -m "Load an episode's file-history snapshot and name every reason it cannot be replayed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 2.5: Restore the starting files and check them against the episode

**Files:**
- Create: `src/culprit/sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `Episode` (episodes); `Tracked` (snapshot).
- Produces:
  - `DEFAULT_FILE_HISTORY = Path.home() / ".claude" / "file-history"`
  - `clone_tree(source: Path, destination: Path) -> None`
  - `strip_git_remotes(workdir: Path) -> None`
  - `@dataclass(frozen=True) class RestoreReport` with `workdir: Path`, `restored: int`, `deleted: int`, `missing_backups: tuple[str, ...]`, `outside_cwd: tuple[str, ...]`.
  - `restore(episode: Episode, tracked: Tracked, destination: Path, file_history_root: Path = DEFAULT_FILE_HISTORY) -> RestoreReport`
  - `@dataclass(frozen=True) class RestoreMismatch` with `path: str`, `reason: str`.
  - `check_restore(episode: Episode, workdir: Path) -> tuple[int, list[RestoreMismatch]]` — (files checked, mismatches).

- [ ] **Step 1: Write the failing tests**

`tests/test_sandbox.py`:

```python
import subprocess

from culprit.episodes import episodes_from_session
from culprit.sandbox import check_restore, clone_tree, restore, strip_git_remotes
from records import edit_result, human


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("edited later\n")
    (repo / "created_later.py").write_text("did not exist at the episode start\n")
    (repo / "untouched.txt").write_text("never tracked\n")
    return repo


def make_history(tmp_path, session_id, backups):
    folder = tmp_path / "file-history" / session_id
    folder.mkdir(parents=True)
    for name, content in backups.items():
        (folder / name).write_text(content)
    return tmp_path / "file-history"


def episode_in(write_session, repo, extra=()):
    (episode,) = episodes_from_session(write_session([human("x", "p1", cwd=str(repo)), *extra]))
    return episode


def test_clone_copies_without_touching_the_source(tmp_path):
    repo = make_repo(tmp_path)
    clone_tree(repo, tmp_path / "clone")
    (tmp_path / "clone" / "untouched.txt").write_text("changed in the clone")
    assert (repo / "untouched.txt").read_text() == "never tracked\n"


def test_restore_overlays_backups_and_deletes_files_that_did_not_exist(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {"aaa@v1": "original content\n"})
    episode = episode_in(write_session, repo)
    report = restore(episode, {"src/a.py": "aaa@v1", str(repo / "created_later.py"): None},
                     tmp_path / "restored", file_history_root=history)
    restored = tmp_path / "restored"
    assert (restored / "src" / "a.py").read_text() == "original content\n"
    assert not (restored / "created_later.py").exists()
    assert (restored / "untouched.txt").read_text() == "never tracked\n"
    assert (report.restored, report.deleted) == (1, 1)
    assert (repo / "src" / "a.py").read_text() == "edited later\n"  # the original is untouched


def test_a_missing_backup_is_reported(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {})
    report = restore(episode_in(write_session, repo), {"src/a.py": "gone@v1"}, tmp_path / "restored",
                     file_history_root=history)
    assert report.missing_backups == ("src/a.py",)
    assert report.restored == 0


def test_paths_outside_the_working_directory_are_never_written(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {"evil@v1": "pwned"})
    victim = tmp_path / "victim.txt"
    victim.write_text("safe")
    report = restore(episode_in(write_session, repo),
                     {"../victim.txt": "evil@v1", str(victim): "evil@v1"},
                     tmp_path / "restored", file_history_root=history)
    assert victim.read_text() == "safe"
    assert set(report.outside_cwd) == {"../victim.txt", str(victim)}


def test_git_remotes_are_removed_from_the_restored_copy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@example.com:x/y.git"], check=True)
    clone_tree(repo, tmp_path / "clone")
    strip_git_remotes(tmp_path / "clone")
    remotes = subprocess.run(["git", "-C", str(tmp_path / "clone"), "remote"], capture_output=True, text=True)
    assert remotes.stdout.strip() == ""
    original = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True)
    assert original.stdout.strip() == "origin"


def test_check_restore_compares_each_file_with_its_first_edit(write_session, tmp_path):
    repo = make_repo(tmp_path)
    episode = episode_in(write_session, repo, extra=[
        edit_result("p1", str(repo / "src" / "a.py"), "original content\n"),
        edit_result("p1", str(repo / "src" / "a.py"), "a later state\n"),
        edit_result("p1", str(repo / "untouched.txt"), "something else\n"),
    ])
    workdir = tmp_path / "restored"
    clone_tree(repo, workdir)
    (workdir / "src" / "a.py").write_text("original content\n")
    checked, mismatches = check_restore(episode, workdir)
    assert checked == 2
    assert [m.path for m in mismatches] == [str(repo / "untouched.txt")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.sandbox'`

- [ ] **Step 3: Write the implementation**

`src/culprit/sandbox.py`:

```python
"""Rebuild the files an episode started from, in a throwaway folder, and check the rebuild.

The folder is a clone of the working directory as it is today with every git remote removed; each
file Claude Code backed up is then put back as it was when the episode's prompt arrived. Files it
never edited are as they are today, which every verdict reports.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from culprit.episodes import Episode
from culprit.snapshot import Tracked

DEFAULT_FILE_HISTORY = Path.home() / ".claude" / "file-history"


@dataclass(frozen=True)
class RestoreReport:
    workdir: Path
    restored: int
    deleted: int
    missing_backups: tuple[str, ...]
    outside_cwd: tuple[str, ...]


@dataclass(frozen=True)
class RestoreMismatch:
    path: str
    reason: str


def clone_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(["cp", "-cR", str(source), str(destination)], capture_output=True, text=True)
    if cloned.returncode != 0:  # not APFS, or cp without -c: fall back to a real copy
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination, symlinks=True)


def strip_git_remotes(workdir: Path) -> None:
    if not (workdir / ".git").exists():
        return
    remotes = subprocess.run(["git", "-C", str(workdir), "remote"], capture_output=True, text=True, check=True)
    for remote in remotes.stdout.split():
        subprocess.run(["git", "-C", str(workdir), "remote", "remove", remote], capture_output=True, check=True)


def restore(episode: Episode, tracked: Tracked, destination: Path,
            file_history_root: Path = DEFAULT_FILE_HISTORY) -> RestoreReport:
    cwd = Path(episode.cwd)
    clone_tree(cwd, destination)
    strip_git_remotes(destination)
    backups = file_history_root / episode.session_id
    restored = deleted = 0
    missing: list[str] = []
    outside: list[str] = []
    for tracked_path, backup_name in tracked.items():
        target = _target_inside(cwd, destination, tracked_path)
        if target is None:
            outside.append(tracked_path)
            continue
        if backup_name is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
                deleted += 1
            continue
        backup = backups / backup_name
        if not backup.is_file():
            missing.append(tracked_path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup, target)
        restored += 1
    return RestoreReport(destination, restored, deleted, tuple(missing), tuple(outside))


def check_restore(episode: Episode, workdir: Path) -> tuple[int, list[RestoreMismatch]]:
    """Compare each restored file with the episode's first Edit of it, whose result holds the whole file."""
    cwd = Path(episode.cwd)
    seen: set[str] = set()
    checked = 0
    mismatches: list[RestoreMismatch] = []
    for record in episode.records:
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or not isinstance(result.get("originalFile"), str):
            continue
        path = result.get("filePath")
        if not path or path in seen:
            continue
        seen.add(path)
        target = _target_inside(cwd, workdir, path)
        if target is None:
            continue
        checked += 1
        if not target.is_file():
            mismatches.append(RestoreMismatch(path, "missing after restore"))
        elif target.read_text(errors="replace") != result["originalFile"]:
            mismatches.append(RestoreMismatch(path, "differs from the file the episode's first edit saw"))
    return checked, mismatches


def _target_inside(cwd: Path, destination: Path, tracked_path: str) -> Path | None:
    path = Path(tracked_path)
    if path.is_absolute():
        try:
            path = path.relative_to(cwd)
        except ValueError:
            return None
    target = (destination / path).resolve()
    if not target.is_relative_to(destination.resolve()):
        return None
    return target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 51 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/sandbox.py tests/test_sandbox.py
git commit -m "Restore an episode's starting files in a throwaway clone and check them against the episode

Backups are overlaid onto an APFS clone with git remotes removed; paths
that resolve outside the clone are never written. check_restore compares
each file with the originalFile its first Edit recorded.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 2.6: `culprit criterion` and `culprit restore`, proven on a real episode

**Files:**
- Modify: `src/culprit/cli.py` (whole file shown)
- Test: `tests/test_cli_stage2.py`

**Interfaces:**
- Consumes: everything above plus Stage 1's `cli`.
- Produces: subcommands `criterion draft|show|approve <episode_id>` and `restore <episode_id> [--dest DIR]`. Every handler has the signature `handler(args: argparse.Namespace) -> int`, attached with `set_defaults(handler=…)`; later stages add subcommands the same way.

`restore` refuses (exit 1, reasons on stderr) when `replay_blockers` is non-empty. Its default destination is `runs/<episode>/pristine`, the folder Stage 3 clones for every replay.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_stage2.py`:

```python
import json

import culprit.cli as cli
from culprit.criterion import load_criterion
from fakes import FakeAsk
from records import edit_result, human, snapshot


def projects_with_one_episode(write_session, tmp_path, repo):
    write_session([snapshot("u-p1", {"a.py": "aaa@v1"}), human("fix a", "p1", cwd=str(repo)),
                   edit_result("p1", str(repo / "a.py"), "old\n"), human("no, still broken", "p2", cwd=str(repo))],
                  name="s1.jsonl")
    return tmp_path / "projects"


def test_criterion_draft_show_approve(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    fake = FakeAsk({"success_means": "a.py works", "checks": [], "must_not": []})
    monkeypatch.setattr(cli, "ask_claude", fake)
    assert cli.main(["criterion", "draft", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 0
    assert "still broken" in fake.calls[0][0]
    assert cli.main(["criterion", "show", "s1:p1", "--runs", str(runs)]) == 0
    assert "a.py works" in capsys.readouterr().out
    assert cli.main(["criterion", "approve", "s1:p1", "--runs", str(runs)]) == 0
    assert load_criterion(runs, "s1:p1").approved is True


def test_restore_builds_the_pristine_copy_and_reports_the_check(write_session, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    exit_code = cli.main(["restore", "s1:p1", "--projects", str(projects), "--runs", str(runs),
                          "--file-history", str(tmp_path / "file-history")])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert (runs / "s1__p1" / "pristine" / "a.py").read_text() == "old\n"
    assert "1 restored" in out and "1 of 1 checked files match" in out


def test_restore_refuses_an_unreplayable_episode(write_session, tmp_path, capsys):
    projects = projects_with_one_episode(write_session, tmp_path, tmp_path / "gone")
    exit_code = cli.main(["restore", "s1:p1", "--projects", str(projects), "--runs", str(tmp_path / "runs")])
    assert exit_code == 1
    assert "no longer exists" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_stage2.py -v`
Expected: FAIL — `argparse` error `invalid choice: 'criterion'` (SystemExit 2).

- [ ] **Step 3: Rewrite `src/culprit/cli.py` with one handler per subcommand**

```python
"""The culprit command line. Each subcommand is one handler: handler(args) -> exit code."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from culprit.claude import ClaudeCallFailed, ask_claude
from culprit.criterion import approve_criterion, draft_criterion, load_criterion, save_criterion
from culprit.eligibility import replay_blockers
from culprit.episodes import episodes_from_session, session_files
from culprit.locate import EpisodeNotFound, find_episode, follow_up_prompts
from culprit.sandbox import DEFAULT_FILE_HISTORY, check_restore, restore
from culprit.signals import FailureSignals
from culprit.snapshot import load_snapshot
from culprit.store import UnknownEpisode, episode_dir, record_symptom, save_candidates
from culprit.taxonomy import Symptom
from culprit.triage import EpisodeSummary, rank_candidates

DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_RUNS = Path("runs")
PRISTINE_DIR = "pristine"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (UnknownEpisode, EpisodeNotFound, FileNotFoundError, ClaudeCallFailed) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", description="Find what to change when a Claude Code episode fails.")
    commands = parser.add_subparsers(dest="command", required=True)

    triage = commands.add_parser("triage", help="rank episodes most likely to have failed")
    _projects_and_runs(triage)
    triage.add_argument("--top", type=int, default=20)
    triage.set_defaults(handler=triage_command)

    confirm = commands.add_parser("confirm", help="record what went wrong in a queued episode")
    confirm.add_argument("episode_id")
    confirm.add_argument("symptom", choices=[s.value for s in Symptom])
    confirm.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    confirm.set_defaults(handler=confirm_command)

    criterion = commands.add_parser("criterion", help="draft, show or approve what success means for an episode")
    criterion.add_argument("action", choices=["draft", "show", "approve"])
    criterion.add_argument("episode_id")
    _projects_and_runs(criterion)
    criterion.set_defaults(handler=criterion_command)

    restore_parser = commands.add_parser("restore", help="rebuild an episode's starting files and check them")
    restore_parser.add_argument("episode_id")
    _projects_and_runs(restore_parser)
    restore_parser.add_argument("--file-history", type=Path, default=DEFAULT_FILE_HISTORY)
    restore_parser.add_argument("--dest", type=Path, default=None)
    restore_parser.set_defaults(handler=restore_command)
    return parser


def _projects_and_runs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)


def triage_command(args: argparse.Namespace) -> int:
    summaries = [
        EpisodeSummary.from_episode(episode)
        for session_file in session_files(args.projects)
        for episode in episodes_from_session(session_file)
    ]
    candidates = rank_candidates(summaries, args.top)
    path = save_candidates(args.runs, candidates)
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


def confirm_command(args: argparse.Namespace) -> int:
    path = record_symptom(args.runs, args.episode_id, Symptom(args.symptom))
    print(f"recorded {args.symptom} for {args.episode_id} in {path}")
    return 0


def criterion_command(args: argparse.Namespace) -> int:
    if args.action == "draft":
        episode = find_episode(args.projects, args.episode_id)
        follow_ups = follow_up_prompts(args.projects, args.episode_id)
        path = save_criterion(args.runs, args.episode_id, draft_criterion(episode, follow_ups, ask=ask_claude))
        print(f"drafted {path} — edit it if needed, then run `culprit criterion approve {args.episode_id}`")
        return 0
    if args.action == "approve":
        approve_criterion(args.runs, args.episode_id)
        print(f"approved the criterion for {args.episode_id}")
        return 0
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None:
        raise FileNotFoundError(f"no criterion for {args.episode_id} — run `culprit criterion draft {args.episode_id}` first")
    state = "approved" if criterion.approved else "draft, not approved"
    print(f"[{state}]\nsuccess means: {criterion.success_means}")
    for check in criterion.checks:
        print(f"check: {check}")
    for rule in criterion.must_not:
        print(f"must not: {rule}")
    return 0


def restore_command(args: argparse.Namespace) -> int:
    episode = find_episode(args.projects, args.episode_id)
    tracked = load_snapshot(episode)
    blockers = replay_blockers(episode, tracked)
    if blockers:
        print(f"culprit: {args.episode_id} cannot be replayed: " + "; ".join(blockers), file=sys.stderr)
        return 1
    destination = args.dest or episode_dir(args.runs, args.episode_id) / PRISTINE_DIR
    if destination.exists():
        print(f"culprit: {destination} already exists — delete it to restore again", file=sys.stderr)
        return 1
    report = restore(episode, tracked, destination, file_history_root=args.file_history)
    checked, mismatches = check_restore(episode, destination)
    print(f"{report.restored} restored, {report.deleted} deleted, {len(report.missing_backups)} missing backups, "
          f"{len(report.outside_cwd)} outside the working directory → {destination}")
    print(f"{checked - len(mismatches)} of {checked} checked files match what the episode saw")
    for mismatch in mismatches:
        print(f"  mismatch: {mismatch.path} ({mismatch.reason})")
    return 0
```

Note: Stage 1's `_triage` and `_confirm` are now `triage_command` and `confirm_command`; Stage 1's three CLI tests still pass unchanged because they only call `main`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 54 passed (Stage 1's `test_cli.py` included).

- [ ] **Step 5: Prove it on a real episode**

Pick a confirmed episode from Stage 1 that is replayable. This script prints the first ten queued candidates with no blockers and at least one checkable edit:

```bash
uv run python - <<'EOF'
import json
from pathlib import Path
from culprit.locate import find_episode
from culprit.snapshot import load_snapshot
from culprit.eligibility import replay_blockers
projects = Path.home() / ".claude" / "projects"
shown = 0
for row in json.loads(Path("runs/candidates.json").read_text()):
    episode = find_episode(projects, row["summary"]["episode_id"])
    edits = sum(1 for r in episode.records if isinstance(r.get("toolUseResult"), dict) and "originalFile" in r["toolUseResult"])
    if not replay_blockers(episode, load_snapshot(episode)) and edits:
        print(episode.episode_id, edits, row["summary"]["prompt_excerpt"]); shown += 1
    if shown == 10: break
EOF
```

Then, with one id `<ID>` from that list:

```bash
uv run culprit confirm <ID> wrong-result      # or the symptom that matches
uv run culprit criterion draft <ID> && uv run culprit criterion show <ID>
uv run culprit restore <ID>
```

Expected:
- `criterion show` prints a success definition that names an outcome, not a fix. If it names a fix, the drafting prompt failed its own rule: record the example in the commit message and tighten `drafting_prompt`.
- `restore` prints `N of N checked files match what the episode saw`. **Any mismatch means the `backupFileName: null` reading or the last-snapshot-wins rule is wrong:** stop, inspect the mismatched file's snapshot entries, and fix `load_snapshot` before Stage 3.
- `ls runs/<ID with : as __>/pristine` shows the project; `git -C` on it (if it is a repo) lists no remotes.

Record the episode id, the match count and the check result in the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/cli.py tests/test_cli_stage2.py
git commit -m "Add culprit criterion and culprit restore; one handler per subcommand

Proven on episode <ID>: <checked> of <checked> restored files match the
originalFile its first Edit recorded.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```
