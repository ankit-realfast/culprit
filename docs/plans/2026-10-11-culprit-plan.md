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

---

## Stage 3 — Replay and judge

**Goal:** `culprit baseline <id>` re-runs a restored episode unchanged N times through a locked-down `claude -p`, judges each run against the approved criterion, and records the baseline pass rate every verdict is measured against.

**Architecture:** Three modules. `replay` owns the settings a replay runs with, the exact command, and one run (fresh clone → optional setup script → `claude -p` → checks → diff → cleanup). `judge` turns one run into pass/fail: a failing check or an errored run fails without a model call; otherwise one schema-checked call decides. `experiment` runs one variant N times under a spend cap and stores the outcome. Stage 4 reuses all three for every oracle.

**Spec:** §3 *Every replay runs locked down* and *Which episodes are replayable*; §4 guard 3 (baseline before variants).

### Global Constraints

- The replay command always carries: `--output-format stream-json --verbose`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, `--permission-prompts none`, `--permission-mode <mode>`, `--no-session-persistence`, `--max-budget-usd <cap>`, and `--disallowedTools "Bash(git push *)" "Bash(gh *)"`.
- Permission mode: the episode's own, except `default` (or none recorded) becomes `acceptEdits`.
- **Each replay runs in a fresh clone placed beside the original working directory** (`<original cwd's parent>/.culprit-replay-<12 hex>`), so Claude Code loads the same ancestor `CLAUDE.md` files the original session loaded; the clone is deleted when the run ends, success or failure.
- After every run culprit deletes the `~/.claude/projects/<encoded workdir>/` folder Claude Code creates, but only when it holds nothing except an empty `memory/` folder.
- Default caps: $2.00 per replay, $10.00 per `baseline` command; a judge model call counts as its $0.50 cap against the episode budget (conservative, because `ask_claude` does not report its cost).
- N defaults to 5.

### Facts this stage relies on (measured 2026-10-11)

- `--output-format stream-json` with `-p` fails without `--verbose` (`Error: When using --print, --output-format=stream-json requires --verbose`).
- The last stream line is `{"type": "result", "subtype": "success", "is_error": false, "result": "<final text>", "total_cost_usd": <float>, "num_turns": <int>, …}`; earlier lines include `system/hook_started`, `system/init`, `assistant`.
- Claude Code names a project folder by replacing every character of the absolute working directory that is not a letter or digit with `-` (`/private/var/folders/kh/…/T/tmp.hIyMkHhkqk` → `-private-var-folders-kh-…-T-tmp-hIyMkHhkqk`).
- Known fidelity differences, reported with every verdict: auto-memory of the original project is not loaded (the replay's working directory is new), and files Claude Code never edited are as they are today.

### Review Focus

1. **A replay that hangs past its timeout** must be recorded as an errored run and its clone still deleted. Pinned in Task 3.1 (`test_a_timed_out_replay_is_an_error_and_cleans_up`).
2. **A replay whose output has no result line** (crash, killed process) must be an errored run, never a pass. Pinned in Task 3.1 (`test_output_without_a_result_line_is_an_error`).
3. **Cleanup must never delete a project folder holding a real session log.** Pinned in Task 3.1 (`test_a_project_folder_with_a_session_log_is_kept`).
4. **The budget must stop before the run that would cross it**, not after. Pinned in Task 3.3 (`test_the_budget_stops_before_the_run_that_would_cross_it`).
5. **`baseline` before restore or before approval** must name the missing step. Pinned in Task 3.4 (`test_baseline_names_the_missing_step`).

### File Structure

```
src/culprit/
├── replay.py        # ReplaySettings, baseline_settings, replay_command, run_replay, replay_workdir
├── judge.py         # Judgement, judge_replay
├── experiment.py    # EpisodeBudget, VariantOutcome, run_variant, load_outcome
└── cli.py           # MODIFY: add `baseline`
tests/
├── fakes.py         # MODIFY: add FakeRun
├── test_replay.py
├── test_judge.py
├── test_experiment.py
└── test_cli_stage3.py
```

---

#### Task 3.1: Run one locked-down replay in a fresh clone

**Files:**
- Create: `src/culprit/replay.py`
- Modify: `tests/fakes.py` (append `FakeRun`)
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `Episode` (episodes); `NO_MCP_SERVERS` (claude); `clone_tree` (sandbox).
- Produces:
  - `SAFETY_DENIED_TOOLS = ("Bash(git push *)", "Bash(gh *)")`, `REPLAY_TIMEOUT_S = 1800`, `DEFAULT_REPLAY_BUDGET_USD = 2.0`, `DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"`
  - `@dataclass(frozen=True) class ReplaySettings` with `prompt: str`, `model: str | None`, `permission_mode: str`, `append_system_prompt: str | None = None`, `tools: str | None = None` (None keeps Claude Code's default set), `disallowed_tools: tuple[str, ...] = ()` (restrictions beyond the safety denies), `context_preamble: str | None = None`, `setup_script: Path | None = None`, `max_budget_usd: float = DEFAULT_REPLAY_BUDGET_USD`.
  - `replay_permission_mode(original: str | None) -> str`
  - `baseline_settings(episode: Episode, max_budget_usd: float = DEFAULT_REPLAY_BUDGET_USD) -> ReplaySettings`
  - `full_prompt(settings: ReplaySettings) -> str`
  - `replay_command(settings: ReplaySettings) -> list[str]`
  - `replay_workdir(episode: Episode) -> Path`
  - `project_folder_name(path: Path) -> str`
  - `@dataclass(frozen=True) class CheckResult` with `command: str`, `exit_code: int`, `output_tail: str`.
  - `@dataclass(frozen=True) class ReplayResult` with `run_dir: Path`, `exit_code: int`, `is_error: bool`, `result_text: str`, `cost_usd: float`, `num_turns: int`, `checks: tuple[CheckResult, ...]`.
  - `class SetupScriptFailed(RuntimeError)`
  - `run_replay(settings: ReplaySettings, pristine: Path, run_dir: Path, workdir: Path, checks: tuple[str, ...], run=subprocess.run, projects_dir: Path = DEFAULT_PROJECTS) -> ReplayResult` — writes `transcript.jsonl`, `stderr.txt`, `diff.patch`, `result.json` into `run_dir`.

- [ ] **Step 1: Add the fake process runner**

Append to `tests/fakes.py`:

```python
class FakeRun:
    """Stands in for subprocess.run in replay tests: answers claude, checks, setup scripts and diff."""

    def __init__(self, claude_stdout="", check_codes=None, diff="", on_claude=None, claude_raises=None,
                 setup_code=0):
        self.claude_stdout = claude_stdout
        self.check_codes = check_codes or {}
        self.diff = diff
        self.on_claude = on_claude
        self.claude_raises = claude_raises
        self.setup_code = setup_code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "claude":
            if self.on_claude:
                self.on_claude(kwargs["cwd"])
            if self.claude_raises:
                raise self.claude_raises
            return completed(self.claude_stdout)
        if command[:2] == ["bash", "-c"]:
            return completed("check output", returncode=self.check_codes.get(command[2], 0))
        if command[0] == "bash":
            return completed("", stderr="setup failed" if self.setup_code else "", returncode=self.setup_code)
        if command[0] == "diff":
            return completed(self.diff, returncode=1 if self.diff else 0)
        raise AssertionError(f"unexpected command {command}")
```

- [ ] **Step 2: Write the failing tests**

`tests/test_replay.py`:

```python
import json
import subprocess
from pathlib import Path

import pytest

from culprit.episodes import episodes_from_session
from culprit.replay import (ReplaySettings, SetupScriptFailed, baseline_settings, project_folder_name,
                            replay_command, replay_permission_mode, replay_workdir, run_replay)
from fakes import FakeRun
from records import assistant, human

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                     "total_cost_usd": 0.42, "num_turns": 7})
STREAM = "\n".join([json.dumps({"type": "system", "subtype": "init"}), json.dumps({"type": "assistant"}), RESULT])


def settings(**overrides):
    base = dict(prompt="fix the bug", model="claude-opus-5", permission_mode="auto")
    base.update(overrides)
    return ReplaySettings(**base)


def pristine_tree(tmp_path):
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "a.py").write_text("old\n")
    return pristine


def test_permission_mode_keeps_auto_and_accept_edits_and_promotes_default():
    assert replay_permission_mode("auto") == "auto"
    assert replay_permission_mode("acceptEdits") == "acceptEdits"
    assert replay_permission_mode("default") == "acceptEdits"
    assert replay_permission_mode(None) == "acceptEdits"


def test_baseline_settings_copy_the_episode(write_session):
    (episode,) = episodes_from_session(write_session([human("fix it", "p1", permissionMode="default"),
                                                      assistant(model="claude-opus-4-8")]))
    s = baseline_settings(episode, max_budget_usd=1.5)
    assert (s.prompt, s.model, s.permission_mode, s.max_budget_usd) == ("fix it", "claude-opus-4-8", "acceptEdits", 1.5)
    assert (s.append_system_prompt, s.tools, s.disallowed_tools, s.context_preamble, s.setup_script) == (None, None, (), None, None)


def test_the_command_is_locked_down_and_carries_every_setting():
    command = replay_command(settings(append_system_prompt="verify first", tools="default",
                                      disallowed_tools=("WebFetch",), context_preamble="FILES"))
    joined = " ".join(command)
    assert command[:3] == ["claude", "-p", "FILES\n\nfix the bug"]
    for required in ["--output-format stream-json", "--verbose", "--strict-mcp-config",
                     '--mcp-config {"mcpServers":{}}', "--permission-prompts none", "--permission-mode auto",
                     "--no-session-persistence", "--max-budget-usd 2.00", "--model claude-opus-5",
                     "--tools default", "--append-system-prompt verify first"]:
        assert required in joined
    denied = command[command.index("--disallowedTools") + 1:]
    assert denied[:3] == ["Bash(git push *)", "Bash(gh *)", "WebFetch"]


def test_replay_workdir_sits_beside_the_original(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("x", "p1", cwd=str(tmp_path / "proj"))]))
    workdir = replay_workdir(episode)
    assert workdir.parent == tmp_path
    assert workdir.name.startswith(".culprit-replay-")
    assert replay_workdir(episode) != workdir


def test_project_folder_name_matches_claude_codes_encoding():
    assert project_folder_name(Path("/Users/akp/Downloads/New-Website")) == "-Users-akp-Downloads-New-Website"
    assert project_folder_name(Path("/private/var/T/tmp.hIy")) == "-private-var-T-tmp-hIy"


def test_a_run_records_everything_runs_checks_and_cleans_up(tmp_path):
    pristine = pristine_tree(tmp_path)
    projects = tmp_path / "projects"
    workdir = tmp_path / ".culprit-replay-abc"

    def claude_edits_and_leaves_memory(cwd):
        (cwd / "a.py").write_text("new\n")
        (projects / project_folder_name(cwd.resolve()) / "memory").mkdir(parents=True)

    run = FakeRun(claude_stdout=STREAM, check_codes={"pytest -q": 1}, diff="--- a.py\n+++ a.py\n",
                  on_claude=claude_edits_and_leaves_memory)
    result = run_replay(settings(), pristine, tmp_path / "run0", workdir, ("pytest -q", "true"), run=run,
                        projects_dir=projects)
    assert (result.is_error, result.result_text, result.cost_usd, result.num_turns) == (False, "done", 0.42, 7)
    assert [(c.command, c.exit_code) for c in result.checks] == [("pytest -q", 1), ("true", 0)]
    assert (tmp_path / "run0" / "transcript.jsonl").read_text() == STREAM
    assert (tmp_path / "run0" / "diff.patch").read_text().startswith("--- a.py")
    assert json.loads((tmp_path / "run0" / "result.json").read_text())["cost_usd"] == 0.42
    assert not workdir.exists()
    assert list(projects.iterdir()) == []
    assert (pristine / "a.py").read_text() == "old\n"
    claude_call = next(kwargs for command, kwargs in run.calls if command[0] == "claude")
    assert claude_call["cwd"] == workdir


def test_a_project_folder_with_a_session_log_is_kept(tmp_path):
    projects = tmp_path / "projects"
    workdir = tmp_path / ".culprit-replay-keep"

    def claude_leaves_a_log(cwd):
        folder = projects / project_folder_name(cwd.resolve())
        (folder / "memory").mkdir(parents=True)
        (folder / "real.jsonl").write_text("{}\n")

    run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", workdir, (),
               run=FakeRun(claude_stdout=RESULT, on_claude=claude_leaves_a_log), projects_dir=projects)
    assert (projects / project_folder_name(workdir.resolve()) / "real.jsonl").exists()


def test_output_without_a_result_line_is_an_error(tmp_path):
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-x", (),
                        run=FakeRun(claude_stdout=json.dumps({"type": "assistant"})), projects_dir=tmp_path / "p")
    assert result.is_error is True
    assert "no result line" in result.result_text


def test_a_timed_out_replay_is_an_error_and_cleans_up(tmp_path):
    workdir = tmp_path / ".culprit-replay-slow"
    run = FakeRun(claude_raises=subprocess.TimeoutExpired(cmd="claude", timeout=1800))
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", workdir, (), run=run,
                        projects_dir=tmp_path / "p")
    assert result.is_error is True
    assert "timed out" in result.result_text
    assert not workdir.exists()


def test_a_failing_setup_script_raises_and_cleans_up(tmp_path):
    workdir = tmp_path / ".culprit-replay-env"
    script = tmp_path / "fix.sh"
    script.write_text("exit 3\n")
    with pytest.raises(SetupScriptFailed, match="setup failed"):
        run_replay(settings(setup_script=script), pristine_tree(tmp_path), tmp_path / "run0", workdir, (),
                   run=FakeRun(setup_code=3), projects_dir=tmp_path / "p")
    assert not workdir.exists()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_replay.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.replay'`

- [ ] **Step 4: Write the implementation**

`src/culprit/replay.py`:

```python
"""One replay: an episode re-run through a locked-down `claude -p` in a fresh clone of its starting files.

The settings carry every knob an oracle may change — prompt, model, appended instructions, tools,
context, setup script, spend cap — so a variant is the baseline with some fields replaced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.claude import NO_MCP_SERVERS
from culprit.episodes import Episode
from culprit.sandbox import clone_tree

SAFETY_DENIED_TOOLS = ("Bash(git push *)", "Bash(gh *)")
REPLAY_TIMEOUT_S = 1800
CHECK_TIMEOUT_S = 600
DEFAULT_REPLAY_BUDGET_USD = 2.0
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
OUTPUT_TAIL_CHARS = 2000
REPLAY_DIR_PREFIX = ".culprit-replay-"


@dataclass(frozen=True)
class ReplaySettings:
    prompt: str
    model: str | None
    permission_mode: str
    append_system_prompt: str | None = None
    tools: str | None = None  # None keeps Claude Code's default tool set
    disallowed_tools: tuple[str, ...] = ()  # restrictions beyond SAFETY_DENIED_TOOLS
    context_preamble: str | None = None
    setup_script: Path | None = None
    max_budget_usd: float = DEFAULT_REPLAY_BUDGET_USD


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    output_tail: str


@dataclass(frozen=True)
class ReplayResult:
    run_dir: Path
    exit_code: int
    is_error: bool
    result_text: str
    cost_usd: float
    num_turns: int
    checks: tuple[CheckResult, ...]


class SetupScriptFailed(RuntimeError):
    pass


def replay_permission_mode(original: str | None) -> str:
    # In the original the human approved prompts; with nobody present, edits are accepted instead.
    return "acceptEdits" if original in (None, "default") else original


def baseline_settings(episode: Episode, max_budget_usd: float = DEFAULT_REPLAY_BUDGET_USD) -> ReplaySettings:
    return ReplaySettings(prompt=episode.prompt_text, model=episode.model,
                          permission_mode=replay_permission_mode(episode.permission_mode),
                          max_budget_usd=max_budget_usd)


def full_prompt(settings: ReplaySettings) -> str:
    if settings.context_preamble:
        return f"{settings.context_preamble}\n\n{settings.prompt}"
    return settings.prompt


def replay_command(settings: ReplaySettings) -> list[str]:
    command = [
        "claude", "-p", full_prompt(settings),
        "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--mcp-config", NO_MCP_SERVERS,
        "--permission-prompts", "none",
        "--permission-mode", settings.permission_mode,
        "--no-session-persistence",
        "--max-budget-usd", f"{settings.max_budget_usd:.2f}",
    ]
    if settings.model:
        command += ["--model", settings.model]
    if settings.tools is not None:
        command += ["--tools", settings.tools]
    if settings.append_system_prompt:
        command += ["--append-system-prompt", settings.append_system_prompt]
    command += ["--disallowedTools", *SAFETY_DENIED_TOOLS, *settings.disallowed_tools]
    return command


def replay_workdir(episode: Episode) -> Path:
    """Beside the original working directory, so the replay loads the same ancestor CLAUDE.md files."""
    return Path(episode.cwd).parent / f"{REPLAY_DIR_PREFIX}{uuid.uuid4().hex[:12]}"


def project_folder_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def run_replay(settings: ReplaySettings, pristine: Path, run_dir: Path, workdir: Path, checks: tuple[str, ...],
               run=subprocess.run, projects_dir: Path = DEFAULT_PROJECTS) -> ReplayResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    clone_tree(pristine, workdir)
    project_folder = projects_dir / project_folder_name(workdir.resolve())
    try:
        if settings.setup_script:
            setup = run(["bash", str(settings.setup_script)], cwd=workdir, capture_output=True, text=True,
                        timeout=CHECK_TIMEOUT_S)
            if setup.returncode != 0:
                raise SetupScriptFailed(f"{settings.setup_script} exited {setup.returncode}: {setup.stderr[-500:]}")
        try:
            completed = run(replay_command(settings), cwd=workdir, capture_output=True, text=True,
                            timeout=REPLAY_TIMEOUT_S)
            stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, exit_code = "", f"timed out after {REPLAY_TIMEOUT_S}s", -1
        (run_dir / "transcript.jsonl").write_text(stdout)
        (run_dir / "stderr.txt").write_text(stderr)
        result_line = _result_line(stdout)
        check_results = tuple(_run_check(command, workdir, run) for command in checks)
        diff = run(["diff", "-ruN", "--exclude=.git", str(pristine), str(workdir)], capture_output=True, text=True)
        (run_dir / "diff.patch").write_text(diff.stdout)
        if result_line is None:
            reason = "timed out" if exit_code == -1 else f"no result line (exit {exit_code})"
            replay = ReplayResult(run_dir, exit_code, True, f"{reason}: {stderr[-300:]}", 0.0, 0, check_results)
        else:
            replay = ReplayResult(run_dir, exit_code, bool(result_line.get("is_error")),
                                  str(result_line.get("result") or ""), float(result_line.get("total_cost_usd") or 0),
                                  int(result_line.get("num_turns") or 0), check_results)
        (run_dir / "result.json").write_text(json.dumps(asdict(replay), default=str, indent=2))
        return replay
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_if_only_empty_memory(project_folder)


def _result_line(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            return record
    return None


def _run_check(command: str, workdir: Path, run) -> CheckResult:
    try:
        done = run(["bash", "-c", command], cwd=workdir, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)
        return CheckResult(command, done.returncode, (done.stdout + done.stderr)[-OUTPUT_TAIL_CHARS:])
    except subprocess.TimeoutExpired:
        return CheckResult(command, -1, f"timed out after {CHECK_TIMEOUT_S}s")


def _remove_if_only_empty_memory(project_folder: Path) -> None:
    if not project_folder.is_dir():
        return
    entries = list(project_folder.iterdir())
    if all(entry.name == "memory" and entry.is_dir() and not any(entry.iterdir()) for entry in entries):
        for entry in entries:
            entry.rmdir()
        project_folder.rmdir()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 64 passed (54 from Stages 1–2 plus 10).

- [ ] **Step 6: Commit**

```bash
git add src/culprit/replay.py tests/fakes.py tests/test_replay.py
git commit -m "Run one locked-down replay in a fresh clone beside the original working directory

The clone sits beside the original so the same ancestor CLAUDE.md files
load; no MCP server, no permission prompts, no session log, git push and gh
denied, spend cap. Checks and a diff are recorded; the clone and Claude
Code's empty project folder are removed whatever happens.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 3.2: Judge one replay against the criterion

**Files:**
- Create: `src/culprit/judge.py`
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: `Criterion` (criterion); `ReplayResult`, `CheckResult` (replay); `Ask`, `ask_claude` (claude).
- Produces:
  - `JUDGE_CALL_CAP_USD = 0.50`, `MAX_DIFF_CHARS = 60_000`, `JUDGEMENT_SCHEMA: dict`
  - `@dataclass(frozen=True) class Judgement` with `passed: bool`, `reason: str`, `by: str` — `"check"`, `"replay"` or `"model"`.
  - `judging_prompt(criterion: Criterion, replay: ReplayResult, diff: str) -> str`
  - `judge_replay(criterion: Criterion, replay: ReplayResult, ask: Ask = ask_claude) -> Judgement`

- [ ] **Step 1: Write the failing tests**

`tests/test_judge.py`:

```python
from culprit.criterion import Criterion
from culprit.judge import MAX_DIFF_CHARS, Judgement, judge_replay, judging_prompt
from culprit.replay import CheckResult, ReplayResult
from fakes import FakeAsk

CRITERION = Criterion("the export has a header row", ("pytest -q",), ("delete tests",), approved=True)


def replay_in(tmp_path, checks=(), is_error=False, diff="+header\n"):
    run_dir = tmp_path / "run0"
    run_dir.mkdir()
    (run_dir / "diff.patch").write_text(diff)
    return ReplayResult(run_dir, 0, is_error, "I added the header", 0.3, 4, tuple(checks))


def test_a_failing_check_fails_without_asking_a_model(tmp_path):
    ask = FakeAsk()
    judgement = judge_replay(CRITERION, replay_in(tmp_path, [CheckResult("pytest -q", 1, "1 failed")]), ask=ask)
    assert judgement == Judgement(False, "check failed: pytest -q (exit 1)", "check")
    assert ask.calls == []


def test_an_errored_replay_fails_without_asking_a_model(tmp_path):
    ask = FakeAsk()
    judgement = judge_replay(CRITERION, replay_in(tmp_path, is_error=True), ask=ask)
    assert (judgement.passed, judgement.by) == (False, "replay")
    assert ask.calls == []


def test_otherwise_the_model_decides_from_criterion_answer_and_diff(tmp_path):
    ask = FakeAsk({"passed": True, "reason": "header present, tests intact"})
    judgement = judge_replay(CRITERION, replay_in(tmp_path, [CheckResult("pytest -q", 0, "ok")]), ask=ask)
    assert judgement == Judgement(True, "header present, tests intact", "model")
    prompt = ask.calls[0][0]
    for expected in ["the export has a header row", "delete tests", "I added the header", "+header", "pytest -q"]:
        assert expected in prompt


def test_a_huge_diff_is_truncated_in_the_prompt(tmp_path):
    replay = replay_in(tmp_path, diff="x" * (MAX_DIFF_CHARS + 5000))
    prompt = judging_prompt(CRITERION, replay, (replay.run_dir / "diff.patch").read_text())
    assert "[diff truncated" in prompt
    assert prompt.count("x") <= MAX_DIFF_CHARS + 100
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_judge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.judge'`

- [ ] **Step 3: Write the implementation**

`src/culprit/judge.py`:

```python
"""Pass or fail for one replay, against the approved criterion.

A failing check or an errored run fails on the spot; only otherwise does a model read the run's final
answer and diff. Every judgement says which of the three decided it.
"""

from __future__ import annotations

from dataclasses import dataclass

from culprit.claude import Ask, ask_claude
from culprit.criterion import Criterion
from culprit.replay import ReplayResult

JUDGE_CALL_CAP_USD = 0.50
MAX_DIFF_CHARS = 60_000
JUDGEMENT_SCHEMA = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["passed", "reason"],
}


@dataclass(frozen=True)
class Judgement:
    passed: bool
    reason: str
    by: str  # "check", "replay" or "model"


def judging_prompt(criterion: Criterion, replay: ReplayResult, diff: str) -> str:
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff truncated: {len(diff) - MAX_DIFF_CHARS} more characters]"
    must_not = "\n".join(f"- {rule}" for rule in criterion.must_not) or "- (nothing listed)"
    checks = "\n".join(f"- {c.command}: exit {c.exit_code}" for c in replay.checks) or "- (no checks)"
    return f"""Decide whether one attempt by a coding agent met its success criterion.

Success means: {criterion.success_means}

A correct attempt must not:
{must_not}

Checks run after the attempt:
{checks}

The agent's final answer:
<answer>{replay.result_text}</answer>

Every file change the attempt made:
<diff>
{diff}
</diff>

Judge only against the criterion. passed is true only if the outcome is met and nothing forbidden
was done. reason is one sentence naming the evidence."""


def judge_replay(criterion: Criterion, replay: ReplayResult, ask: Ask = ask_claude) -> Judgement:
    failed = [check for check in replay.checks if check.exit_code != 0]
    if failed:
        return Judgement(False, f"check failed: {failed[0].command} (exit {failed[0].exit_code})", "check")
    if replay.is_error:
        return Judgement(False, f"the replay ended in an error: {replay.result_text[:300]}", "replay")
    diff = (replay.run_dir / "diff.patch").read_text(errors="replace")
    answer = ask(judging_prompt(criterion, replay, diff), JUDGEMENT_SCHEMA)
    return Judgement(bool(answer["passed"]), str(answer["reason"]), "model")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 68 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/judge.py tests/test_judge.py
git commit -m "Judge one replay: failing check or errored run fails; otherwise a model reads answer and diff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 3.3: Run one variant N times under a spend cap

**Files:**
- Create: `src/culprit/experiment.py`
- Test: `tests/test_experiment.py`

**Interfaces:**
- Consumes: `Episode` (episodes); `Criterion` (criterion); `ReplaySettings`, `run_replay`, `replay_workdir` (replay); `Judgement`, `judge_replay`, `JUDGE_CALL_CAP_USD` (judge); `episode_dir` (store).
- Produces:
  - `REPLAYS_DIR = "replays"`, `PRISTINE_DIR = "pristine"`, `OUTCOME_FILE = "outcome.json"`
  - `class BudgetExhausted(RuntimeError)`
  - `@dataclass class EpisodeBudget` with `cap_usd: float`, `spent_usd: float = 0.0`, methods `ensure_room(next_cap_usd: float) -> None` and `spend(usd: float) -> None`.
  - `@dataclass(frozen=True) class VariantOutcome` with `variant: str`, `passes: int`, `runs: int`, `cost_usd: float`.
  - `run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path, criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay, out_dir: Path | None = None) -> VariantOutcome` — runs go to `<out_dir or episode folder>/replays/<variant>/<i>/`, each with `judgement.json`; the outcome to `replays/<variant>/outcome.json`. `out_dir` lets Stage 5's selftest keep planted runs apart from real ones.
  - `load_outcome(folder: Path, variant: str) -> VariantOutcome | None` — `folder` is the episode folder (or a selftest folder).

- [ ] **Step 1: Write the failing tests**

`tests/test_experiment.py`:

```python
import json

import pytest

from culprit.criterion import Criterion
from culprit.episodes import episodes_from_session
from culprit.experiment import BudgetExhausted, EpisodeBudget, VariantOutcome, load_outcome, run_variant
from culprit.judge import Judgement
from culprit.replay import ReplayResult, ReplaySettings
from culprit.store import episode_dir
from records import human

CRITERION = Criterion("works", (), (), approved=True)
SETTINGS = ReplaySettings(prompt="fix", model="claude-opus-5", permission_mode="auto", max_budget_usd=1.0)


def episode_at(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path / "proj"))]))
    return episode


def fake_replay(cost=0.25):
    calls = []

    def replay(settings, pristine, run_dir, workdir, checks):
        calls.append((settings, pristine, run_dir, workdir, checks))
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", cost, 3, ())

    replay.calls = calls
    return replay


def judge_sequence(*passed):
    answers = list(passed)
    return lambda criterion, result: Judgement(answers.pop(0), "r", "model")


def test_a_variant_runs_n_times_and_stores_each_judgement_and_the_outcome(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    runs = tmp_path / "runs"
    replay = fake_replay()
    outcome = run_variant("baseline", SETTINGS, n=3, episode=episode, runs_dir=runs, criterion=CRITERION,
                          budget=EpisodeBudget(10.0), replay=replay, judge=judge_sequence(True, False, True))
    assert outcome == VariantOutcome("baseline", passes=2, runs=3, cost_usd=0.75)
    folder = episode_dir(runs, episode.episode_id)
    assert json.loads((folder / "replays" / "baseline" / "1" / "judgement.json").read_text())["passed"] is False
    assert load_outcome(folder, "baseline") == outcome
    assert all(call[1] == folder / "pristine" for call in replay.calls)
    assert all(call[3].parent == tmp_path and call[3].name.startswith(".culprit-replay-") for call in replay.calls)


def test_the_budget_stops_before_the_run_that_would_cross_it(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    budget = EpisodeBudget(cap_usd=3.2)
    replay = fake_replay(cost=1.0)
    with pytest.raises(BudgetExhausted, match="3.20"):
        run_variant("baseline", SETTINGS, n=5, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                    budget=budget, replay=replay, judge=judge_sequence(*[True] * 5))
    # each run reserves its $1.00 cap plus the $0.50 judge cap: two runs fit in $3.20, a third would not
    assert len(replay.calls) == 2


def test_out_dir_keeps_runs_apart_from_the_episode_folder(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    out = tmp_path / "selftest" / "weak-model"
    run_variant("baseline", SETTINGS, n=1, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                budget=EpisodeBudget(10.0), replay=fake_replay(), judge=judge_sequence(True), out_dir=out)
    assert load_outcome(out, "baseline").passes == 1
    assert not (episode_dir(tmp_path / "runs", episode.episode_id) / "replays").exists()


def test_a_variant_never_run_loads_as_none(tmp_path):
    assert load_outcome(tmp_path, "model") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_experiment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.experiment'`

- [ ] **Step 3: Write the implementation**

`src/culprit/experiment.py`:

```python
"""One variant of an episode, replayed N times and judged, inside a spend cap.

The outcome — passes out of runs — is what the verdict compares across variants.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.judge import JUDGE_CALL_CAP_USD, judge_replay
from culprit.replay import ReplaySettings, replay_workdir, run_replay
from culprit.store import episode_dir

REPLAYS_DIR = "replays"
PRISTINE_DIR = "pristine"
OUTCOME_FILE = "outcome.json"
JUDGEMENT_FILE = "judgement.json"


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class EpisodeBudget:
    cap_usd: float
    spent_usd: float = 0.0

    def ensure_room(self, next_cap_usd: float) -> None:
        if self.spent_usd + next_cap_usd > self.cap_usd:
            raise BudgetExhausted(f"the next run could spend ${next_cap_usd:.2f}; ${self.spent_usd:.2f} of "
                                  f"${self.cap_usd:.2f} is already spent")

    def spend(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass(frozen=True)
class VariantOutcome:
    variant: str
    passes: int
    runs: int
    cost_usd: float


def run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path,
                criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay,
                out_dir: Path | None = None) -> VariantOutcome:
    episode_folder = episode_dir(runs_dir, episode.episode_id)
    folder = out_dir or episode_folder
    variant_dir = folder / REPLAYS_DIR / variant
    passes = 0
    cost = 0.0
    for i in range(n):
        budget.ensure_room(settings.max_budget_usd + JUDGE_CALL_CAP_USD)
        run_dir = variant_dir / str(i)
        result = replay(settings, episode_folder / PRISTINE_DIR, run_dir, replay_workdir(episode), criterion.checks)
        judgement = judge(criterion, result)
        spent = result.cost_usd + (JUDGE_CALL_CAP_USD if judgement.by == "model" else 0.0)
        budget.spend(spent)
        cost += result.cost_usd
        (run_dir / JUDGEMENT_FILE).write_text(json.dumps(asdict(judgement), indent=2))
        passes += int(judgement.passed)
    outcome = VariantOutcome(variant, passes, n, round(cost, 4))
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / OUTCOME_FILE).write_text(json.dumps(asdict(outcome), indent=2))
    return outcome


def load_outcome(folder: Path, variant: str) -> VariantOutcome | None:
    path = folder / REPLAYS_DIR / variant / OUTCOME_FILE
    if not path.exists():
        return None
    return VariantOutcome(**json.loads(path.read_text()))
```

Note on the budget test: run 1 reserves $1.50 (0 + 1.50 ≤ 3.20) and spends $1.50 (replay $1.00 + judge cap $0.50); run 2 reserves $1.50 (1.50 + 1.50 ≤ 3.20) and spends $1.50; run 3 needs 3.00 + 1.50 > 3.20 and stops.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 72 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/experiment.py tests/test_experiment.py
git commit -m "Run one variant N times inside an episode spend cap and store its pass count

The cap is checked before each run against that run's replay cap plus the
judge call's cap, so a run that could cross the cap never starts.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 3.4: `culprit baseline`, proven on the restored real episode

**Files:**
- Modify: `src/culprit/cli.py`
- Test: `tests/test_cli_stage3.py`

**Interfaces:**
- Consumes: Stage 2's CLI; `baseline_settings` (replay); `EpisodeBudget`, `BudgetExhausted`, `run_variant`, `PRISTINE_DIR` (experiment).
- Produces: subcommand `baseline <episode_id> [--n 5] [--budget 10.0] [--replay-budget 2.0]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_stage3.py`:

```python
import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import VariantOutcome
from culprit.store import episode_dir
from records import human, snapshot


def setup_episode(write_session, tmp_path, approved=True, restored=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=approved))
    if restored:
        (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    return tmp_path / "projects", runs


def test_baseline_runs_the_unchanged_episode_and_prints_the_pass_rate(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = setup_episode(write_session, tmp_path)
    seen = {}

    def fake_run_variant(variant, settings, **kwargs):
        seen.update(variant=variant, settings=settings, n=kwargs["n"], cap=kwargs["budget"].cap_usd)
        return VariantOutcome(variant, 1, kwargs["n"], 3.1)

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    exit_code = cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs), "--n", "4",
                          "--budget", "12"])
    assert exit_code == 0
    assert seen["variant"] == "baseline" and seen["n"] == 4 and seen["cap"] == 12.0
    assert seen["settings"].prompt == "fix a"
    assert "baseline: 1 of 4 passed" in capsys.readouterr().out


def test_baseline_names_the_missing_step(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, approved=False)
    assert cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 1
    assert "culprit criterion approve" in capsys.readouterr().err


def test_baseline_before_restore_names_restore(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, restored=False)
    assert cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 1
    assert "culprit restore" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_stage3.py -v`
Expected: FAIL — `invalid choice: 'baseline'` (SystemExit 2).

- [ ] **Step 3: Add the subcommand**

In `src/culprit/cli.py`, add to the imports:

```python
from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.experiment import BudgetExhausted, EpisodeBudget, run_variant
from culprit.replay import DEFAULT_REPLAY_BUDGET_USD, SetupScriptFailed, baseline_settings
```

Replace the `except` line in `main` with:

```python
    except (UnknownEpisode, EpisodeNotFound, FileNotFoundError, ClaudeCallFailed, BudgetExhausted,
            SetupScriptFailed) as refusal:
```

Add before `return parser` in `build_parser`:

```python
    baseline = commands.add_parser("baseline", help="replay the unchanged episode N times and judge each run")
    baseline.add_argument("episode_id")
    _projects_and_runs(baseline)
    baseline.add_argument("--n", type=int, default=5)
    baseline.add_argument("--budget", type=float, default=10.0, help="spend cap in USD for this command")
    baseline.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    baseline.set_defaults(handler=baseline_command)
```

Add these functions at the end of the file:

```python
def ready_to_replay(args: argparse.Namespace) -> tuple[Episode, Criterion]:
    """The episode and its approved criterion, once restore has built the pristine copy; refuses otherwise."""
    episode = find_episode(args.projects, args.episode_id)
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None or not criterion.approved:
        raise FileNotFoundError(f"no approved criterion for {args.episode_id} — run "
                                f"`culprit criterion draft {args.episode_id}` then `culprit criterion approve {args.episode_id}`")
    if not (episode_dir(args.runs, args.episode_id) / PRISTINE_DIR).is_dir():
        raise FileNotFoundError(f"{args.episode_id} has no restored copy — run `culprit restore {args.episode_id}` first")
    return episode, criterion


def baseline_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    outcome = run_variant("baseline", baseline_settings(episode, args.replay_budget), n=args.n, episode=episode,
                          runs_dir=args.runs, criterion=criterion, budget=EpisodeBudget(args.budget))
    print(f"baseline: {outcome.passes} of {outcome.runs} passed, ${outcome.cost_usd:.2f} spent on replays")
    return 0
```

(`PRISTINE_DIR` is already defined in `cli.py` from Stage 2 with the same value, `"pristine"`, as `culprit.experiment.PRISTINE_DIR`; replace the Stage 2 constant with `from culprit.experiment import PRISTINE_DIR` so one name owns it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 75 passed.

- [ ] **Step 5: Prove it on the real episode restored in Task 2.6**

One run first, so a wiring fault costs one replay, not five:

```bash
uv run culprit baseline <ID> --n 1 --budget 3
```

Expected: `baseline: K of 1 passed, $X spent on replays`, exit 0. Then read what the loop actually wrote, not a proxy:

```bash
R=runs/<ID with : as __>/replays/baseline/0
head -c 600 $R/transcript.jsonl         # system/init present; its cwd is <original parent>/.culprit-replay-…
python3 -c "import json;l=[json.loads(x) for x in open('$R/transcript.jsonl')];i=[x for x in l if x.get('subtype')=='init'][0];print(i.get('cwd'), i.get('mcp_servers'), i.get('permissionMode'))"
cat $R/result.json $R/judgement.json
head -40 $R/diff.patch
ls -d "$(dirname <original cwd>)"/.culprit-replay-* 2>/dev/null | wc -l    # prints 0: the clone is gone
ls ~/.claude/projects | grep -c culprit-replay                              # prints 0: no folder left behind
```

Expected: `mcp_servers` is empty, the permission mode is the mapped one, the judgement's reason matches the diff, and nothing is left behind. If the judgement disagrees with your own reading of the diff, note it: Stage 5 calibration exists for this, but a gross error here means `judging_prompt` needs fixing before Stage 4.

Then the full baseline:

```bash
rm -r runs/<ID with : as __>/replays/baseline
uv run culprit baseline <ID>
```

Expected: `baseline: K of 5 passed`. Record K, the cost and the wall time in the commit message. K ≥ 3 means this episode will be **Not reproducible**; pick another episode for Stage 4's proof.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/cli.py tests/test_cli_stage3.py
git commit -m "Add culprit baseline: replay the unchanged episode N times and judge each run

Proven on <ID>: K of 5 passed, \$<cost>, <wall time>; transcript init shows
no MCP servers and the replay ran beside the original directory.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

## Stage 4 — Oracles and verdict

**Goal:** `culprit diagnose <id>` builds one variant per cause, rejects any that leak the answer, replays each N times, and names the episode's cause by the spec's first-match order — including Not reproducible, Multi-cause (via the pair test) and Unexplained — with every untested cause and its reason recorded.

**Architecture:** `taxonomy` gains `Cause`; the variant names *are* cause values, so one concept has one name. `stats` holds the one-sided Fisher exact test. `leak` asks one schema-checked question per drafted text. `oracles` builds each variant as the baseline settings with that cause's fields replaced, and `combine` merges variants for the pair test. `verdict` owns the decision rules and the stored verdict; `diagnose` runs the sequence, stopping at the first cause that beats the baseline and resuming from saved variants and outcomes after a budget stop.

**Spec:** §1 *Cause*; §3 *The oracles, concretely* and *What "passes clearly" means*; §4 guard 1 (leak check).

### Global Constraints

- Cause values and order, exactly: `misjudged`, `not-reproducible`, `task`, `environment`, `agent-setup`, `harness`, `model`, `multi-cause`, `unexplained`.
- Oracle order: task, environment, agent-setup, harness, model. Oracle model: `claude-fable-5-1`.
- A variant beats the baseline when the one-sided Fisher exact p < 0.05. Not reproducible: baseline passes > runs / 2 (3 of 5).
- Fields each oracle may change, and no others: task → `prompt`; environment → `setup_script`; agent-setup → `append_system_prompt`; harness → `tools`, `disallowed_tools`, `context_preamble`, `max_budget_usd`; model → `model`.
- Harness oracle: `tools="default"`, `disallowed_tools=()` (the safety denies still apply, they live in `replay_command`), the files the episode read or edited preloaded up to 100,000 characters, double the replay cap.
- A drafted task or agent-setup text gets at most 2 attempts; two leaks make that cause untested.
- `diagnose` default spend cap: $40 per episode.

### Review Focus

1. **A budget stop in the middle of a diagnosis** must keep everything already paid for: re-running `diagnose` reuses saved variants and outcomes instead of re-drafting and re-running them. Pinned in Task 4.4 (`test_a_rerun_reuses_saved_variants_and_outcomes`).
2. **An episode the human marked `no-failure`** must get the verdict Misjudged without a single replay. Pinned in Task 4.4 (`test_a_rejected_candidate_is_misjudged_without_replays`).
3. **An episode that already ran on Fable** must report the Model cause as untested, not test Fable against Fable. Pinned in Task 4.3 (`test_the_model_oracle_is_untested_for_an_episode_already_on_fable`).
4. **A leaked draft must never be replayed.** Pinned in Task 4.3 (`test_two_leaks_make_the_cause_untested`).
5. **Only one tested oracle and it failed** — the pair test cannot run, so the verdict is Unexplained, not a crash on an empty pair list. Pinned in Task 4.4 (`test_one_tested_oracle_that_fails_is_unexplained`).

### File Structure

```
src/culprit/
├── taxonomy.py      # MODIFY: add Cause, ORACLE_ORDER
├── stats.py         # fisher_one_sided
├── leak.py          # LeakCheck, check_leak
├── sandbox.py       # MODIFY: rename _target_inside → target_inside (public; oracles reuse it)
├── replay.py        # MODIFY: add settings_to_json, settings_from_json
├── oracles.py       # VariantBuild, build_variant, harness_context, combine
├── verdict.py       # Verdict, beats, reproducible, first_beating, save_verdict, load_verdict
├── diagnose.py      # diagnose()
└── cli.py           # MODIFY: add env-fix, diagnose
tests/
├── test_stats.py
├── test_leak.py
├── test_oracles.py
├── test_diagnose.py
└── test_cli_stage4.py
```

---

#### Task 4.1: The cause axis and the pass test

**Files:**
- Modify: `src/culprit/taxonomy.py`
- Create: `src/culprit/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Produces:
  - `class Cause(StrEnum)` with `MISJUDGED = "misjudged"`, `NOT_REPRODUCIBLE = "not-reproducible"`, `TASK = "task"`, `ENVIRONMENT = "environment"`, `AGENT_SETUP = "agent-setup"`, `HARNESS = "harness"`, `MODEL = "model"`, `MULTI_CAUSE = "multi-cause"`, `UNEXPLAINED = "unexplained"`.
  - `ORACLE_ORDER: tuple[Cause, ...] = (Cause.TASK, Cause.ENVIRONMENT, Cause.AGENT_SETUP, Cause.HARNESS, Cause.MODEL)`
  - `fisher_one_sided(variant_passes: int, variant_runs: int, baseline_passes: int, baseline_runs: int) -> float`

- [ ] **Step 1: Write the failing tests**

`tests/test_stats.py`:

```python
import math

import pytest

from culprit.stats import fisher_one_sided
from culprit.taxonomy import ORACLE_ORDER, Cause


def test_causes_are_the_spec_values_in_order():
    assert [c.value for c in Cause] == ["misjudged", "not-reproducible", "task", "environment", "agent-setup",
                                         "harness", "model", "multi-cause", "unexplained"]
    assert [c.value for c in ORACLE_ORDER] == ["task", "environment", "agent-setup", "harness", "model"]


@pytest.mark.parametrize("variant, baseline, expected", [
    (4, 0, 6 / 252),    # the spec's example: 4/5 against 0/5 clears p < 0.05
    (3, 0, 21 / 252),   # 3/5 against 0/5 does not
    (5, 0, 1 / 252),
    (5, 1, 6 / 252),
    (0, 0, 1.0),
])
def test_fisher_one_sided_matches_the_hypergeometric_tail(variant, baseline, expected):
    assert fisher_one_sided(variant, 5, baseline, 5) == pytest.approx(expected)


def test_fisher_one_sided_for_equal_rates_is_large():
    expected = sum(math.comb(4, k) * math.comb(6, 5 - k) for k in range(2, 5)) / math.comb(10, 5)
    assert fisher_one_sided(2, 5, 2, 5) == pytest.approx(expected)
    assert expected > 0.5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_stats.py -v`
Expected: FAIL — `ImportError: cannot import name 'ORACLE_ORDER' from 'culprit.taxonomy'`

- [ ] **Step 3: Write the implementation**

Append to `src/culprit/taxonomy.py`:

```python
class Cause(StrEnum):
    """The cause axis: what to change, cheapest fix first; the first cause proven wins."""

    MISJUDGED = "misjudged"
    NOT_REPRODUCIBLE = "not-reproducible"
    TASK = "task"
    ENVIRONMENT = "environment"
    AGENT_SETUP = "agent-setup"
    HARNESS = "harness"
    MODEL = "model"
    MULTI_CAUSE = "multi-cause"
    UNEXPLAINED = "unexplained"


ORACLE_ORDER = (Cause.TASK, Cause.ENVIRONMENT, Cause.AGENT_SETUP, Cause.HARNESS, Cause.MODEL)
```

`src/culprit/stats.py`:

```python
"""The test that decides whether a variant passes clearly more often than the baseline."""

from __future__ import annotations

from math import comb


def fisher_one_sided(variant_passes: int, variant_runs: int, baseline_passes: int, baseline_runs: int) -> float:
    """P(the variant passes at least this often | both pass at the same rate): the hypergeometric upper tail."""
    total_passes = variant_passes + baseline_passes
    total_runs = variant_runs + baseline_runs
    tail = sum(
        comb(total_passes, k) * comb(total_runs - total_passes, variant_runs - k)
        for k in range(variant_passes, min(total_passes, variant_runs) + 1)
    )
    return tail / comb(total_runs, variant_runs)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 82 passed (75 plus 7: one taxonomy test, five parametrised cases, one equal-rates test).

- [ ] **Step 5: Commit**

```bash
git add src/culprit/taxonomy.py src/culprit/stats.py tests/test_stats.py
git commit -m "Add the cause axis in the spec's order and the one-sided Fisher exact test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 4.2: Reject a drafted text that leaks the answer

**Files:**
- Create: `src/culprit/leak.py`
- Test: `tests/test_leak.py`

**Interfaces:**
- Consumes: `Ask`, `ask_claude` (claude).
- Produces:
  - `LEAK_SCHEMA: dict`
  - `@dataclass(frozen=True) class LeakCheck` with `leaks: bool`, `evidence: str`.
  - `leak_prompt(drafted_text: str, original_prompt: str, follow_ups: list[str]) -> str`
  - `check_leak(drafted_text: str, original_prompt: str, follow_ups: list[str], ask: Ask = ask_claude) -> LeakCheck`

- [ ] **Step 1: Write the failing tests**

`tests/test_leak.py`:

```python
from culprit.leak import LeakCheck, check_leak, leak_prompt
from fakes import FakeAsk


def test_the_prompt_carries_the_draft_the_original_and_the_later_messages():
    prompt = leak_prompt("Fix login; empty passwords must be rejected.", "fix login",
                         ["no — the bug is in validate() in auth.py"])
    for expected in ["Fix login; empty passwords must be rejected.", "fix login", "validate() in auth.py",
                     "Stating the outcome the person wanted is allowed"]:
        assert expected in prompt


def test_the_answer_becomes_a_leak_check():
    ask = FakeAsk({"leaks": True, "evidence": "names validate() in auth.py"})
    assert check_leak("Fix validate() in auth.py", "fix login", ["it's validate() in auth.py"], ask=ask) == \
        LeakCheck(True, "names validate() in auth.py")
    assert ask.calls[0][1]["required"] == ["leaks", "evidence"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_leak.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.leak'`

- [ ] **Step 3: Write the implementation**

`src/culprit/leak.py`:

```python
"""The leak check: an oracle may add only what its actor could have known before starting.

A drafted task or instruction that carries a detail found only in the person's later messages would
make the replay pass for the wrong reason, so it is rejected before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from culprit.claude import Ask, ask_claude

LEAK_SCHEMA = {
    "type": "object",
    "properties": {"leaks": {"type": "boolean"}, "evidence": {"type": "string"}},
    "required": ["leaks", "evidence"],
}


@dataclass(frozen=True)
class LeakCheck:
    leaks: bool
    evidence: str


def leak_prompt(drafted_text: str, original_prompt: str, follow_ups: list[str]) -> str:
    later = "\n".join(f"<message>{text}</message>" for text in follow_ups) or "<message>(none)</message>"
    return f"""A drafted text will be given to a coding agent before it starts a task. Decide whether the draft
leaks the answer.

The original request:
<request>{original_prompt}</request>

What the person said later, after seeing the agent's attempt:
<later>
{later}
</later>

The draft:
<draft>{drafted_text}</draft>

leaks is true if the draft contains a specific detail that appears only in the later messages and that
the agent could not have known from the original request: a named fix, root cause, file, function,
value or command. Stating the outcome the person wanted is allowed. evidence quotes the leaked detail,
or says "none"."""


def check_leak(drafted_text: str, original_prompt: str, follow_ups: list[str], ask: Ask = ask_claude) -> LeakCheck:
    answer = ask(leak_prompt(drafted_text, original_prompt, follow_ups), LEAK_SCHEMA)
    return LeakCheck(bool(answer["leaks"]), str(answer["evidence"]))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 84 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/leak.py tests/test_leak.py
git commit -m "Reject an oracle draft that carries a detail found only in the person's later messages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 4.3: Build each oracle variant

**Files:**
- Modify: `src/culprit/sandbox.py` (rename `_target_inside` → `target_inside` at its definition and its two call sites), `src/culprit/replay.py` (append `settings_to_json`, `settings_from_json`)
- Create: `src/culprit/oracles.py`
- Test: `tests/test_oracles.py`

**Interfaces:**
- Consumes: `Episode`, `tool_uses` (episodes); `Criterion` (criterion); `ReplaySettings` (replay); `target_inside` (sandbox); `check_leak` (leak); `Ask`, `ask_claude` (claude); `Cause`, `Symptom` (taxonomy).
- Produces:
  - In `replay.py`: `settings_to_json(settings: ReplaySettings) -> dict` and `settings_from_json(data: dict) -> ReplaySettings` (`setup_script` stored as a string or null; tuples as lists).
  - `ORACLE_MODEL = "claude-fable-5-1"`, `MAX_CONTEXT_CHARS = 100_000`, `MAX_DRAFT_ATTEMPTS = 2`, `FILE_TOOLS: tuple[str, ...]`, `REWRITE_SCHEMA: dict`
  - `VARIANT_FIELDS: dict[Cause, tuple[str, ...]]`
  - `@dataclass(frozen=True) class VariantBuild` with `settings: ReplaySettings | None`, `untested_reason: str | None`.
  - `task_rewrite_prompt(episode: Episode, criterion: Criterion, rejected_evidence: str | None) -> str`
  - `agent_instructions_prompt(criterion: Criterion, symptom: Symptom, rejected_evidence: str | None) -> str`
  - `harness_context(episode: Episode, pristine: Path) -> str`
  - `build_variant(cause: Cause, base: ReplaySettings, *, episode: Episode, follow_ups: list[str], criterion: Criterion, symptom: Symptom, pristine: Path, env_script: Path | None, ask: Ask = ask_claude) -> VariantBuild`
  - `combine(base: ReplaySettings, variants: dict[Cause, ReplaySettings]) -> ReplaySettings`

- [ ] **Step 1: Rename the sandbox helper**

In `src/culprit/sandbox.py`, rename `_target_inside` to `target_inside` (definition plus the calls in `restore` and `check_restore`).

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: 6 passed (behaviour unchanged).

- [ ] **Step 2: Write the failing tests**

`tests/test_oracles.py`:

```python
from dataclasses import replace
from pathlib import Path

from culprit.criterion import Criterion
from culprit.episodes import episodes_from_session
from culprit.oracles import (MAX_CONTEXT_CHARS, ORACLE_MODEL, VariantBuild, build_variant, combine,
                             harness_context)
from culprit.replay import ReplaySettings, settings_from_json, settings_to_json
from culprit.taxonomy import Cause, Symptom
from fakes import FakeAsk
from records import human, tool_use

CRITERION = Criterion("empty passwords are rejected with a message", (), ("weaken validation",), approved=True)
BASE = ReplaySettings(prompt="fix login", model="claude-opus-5", permission_mode="auto", max_budget_usd=2.0)


def episode_with(write_session, repo, *uses):
    (episode,) = episodes_from_session(write_session([human("fix login", "p1", cwd=str(repo)), *uses]))
    return episode


def build(cause, episode, tmp_path, ask=None, env_script=None, base=BASE):
    return build_variant(cause, base, episode=episode, follow_ups=["still lets empty passwords in"],
                         criterion=CRITERION, symptom=Symptom.WRONG_RESULT, pristine=tmp_path / "pristine",
                         env_script=env_script, ask=ask or FakeAsk())


def test_settings_round_trip_through_json(tmp_path):
    settings = replace(BASE, disallowed_tools=("WebFetch",), setup_script=tmp_path / "fix.sh")
    assert settings_from_json(settings_to_json(settings)) == settings


def test_the_task_oracle_rewrites_only_the_prompt_after_a_clean_leak_check(write_session, tmp_path):
    ask = FakeAsk({"text": "Fix login so empty passwords are rejected with a message."},
                  {"leaks": False, "evidence": "none"})
    built = build(Cause.TASK, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built == VariantBuild(replace(BASE, prompt="Fix login so empty passwords are rejected with a message."), None)
    assert "empty passwords are rejected" in ask.calls[0][0]
    assert "still lets empty passwords in" in ask.calls[1][0]


def test_a_leaked_draft_is_redrafted_with_the_evidence(write_session, tmp_path):
    ask = FakeAsk({"text": "Fix validate() in auth.py"}, {"leaks": True, "evidence": "names validate()"},
                  {"text": "Fix login so empty passwords are rejected."}, {"leaks": False, "evidence": "none"})
    built = build(Cause.TASK, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings.prompt == "Fix login so empty passwords are rejected."
    assert "names validate()" in ask.calls[2][0]


def test_two_leaks_make_the_cause_untested(write_session, tmp_path):
    ask = FakeAsk({"text": "a"}, {"leaks": True, "evidence": "x"}, {"text": "b"}, {"leaks": True, "evidence": "y"})
    built = build(Cause.AGENT_SETUP, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings is None
    assert "leak" in built.untested_reason


def test_the_agent_setup_oracle_replaces_only_the_appended_instructions(write_session, tmp_path):
    ask = FakeAsk({"text": "Verify every change against its stated outcome before reporting done."},
                  {"leaks": False, "evidence": "none"})
    built = build(Cause.AGENT_SETUP, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings == replace(BASE, append_system_prompt="Verify every change against its stated outcome before reporting done.")
    assert "CLAUDE.md" in ask.calls[0][0]


def test_the_environment_oracle_needs_a_human_script(write_session, tmp_path):
    episode = episode_with(write_session, tmp_path)
    assert build(Cause.ENVIRONMENT, episode, tmp_path).untested_reason.startswith("no environment fix")
    script = tmp_path / "env-fix.sh"
    script.write_text("true\n")
    assert build(Cause.ENVIRONMENT, episode, tmp_path, env_script=script).settings == replace(BASE, setup_script=script)


def test_the_model_oracle_swaps_in_fable(write_session, tmp_path):
    assert build(Cause.MODEL, episode_with(write_session, tmp_path), tmp_path).settings == replace(BASE, model=ORACLE_MODEL)


def test_the_model_oracle_is_untested_for_an_episode_already_on_fable(write_session, tmp_path):
    built = build(Cause.MODEL, episode_with(write_session, tmp_path), tmp_path, base=replace(BASE, model="claude-fable-5"))
    assert built.settings is None
    assert "already ran on Fable" in built.untested_reason


def test_the_harness_oracle_lifts_restrictions_preloads_files_and_doubles_the_cap(write_session, tmp_path):
    repo = tmp_path / "repo"
    pristine = tmp_path / "pristine"
    (pristine / "src").mkdir(parents=True)
    (pristine / "src" / "auth.py").write_text("def validate(): ...\n")
    episode = episode_with(write_session, repo, tool_use("Read", file_path=str(repo / "src" / "auth.py")),
                           tool_use("Edit", file_path="src/auth.py"), tool_use("Read", file_path="/etc/hosts"))
    base = replace(BASE, tools="Read", disallowed_tools=("Edit",))
    built = build_variant(Cause.HARNESS, base, episode=episode, follow_ups=[], criterion=CRITERION,
                          symptom=Symptom.WRONG_RESULT, pristine=pristine, env_script=None, ask=FakeAsk())
    s = built.settings
    assert (s.tools, s.disallowed_tools, s.max_budget_usd, s.prompt) == ("default", (), 4.0, "fix login")
    assert s.context_preamble.count("=== src/auth.py ===") == 1
    assert "def validate()" in s.context_preamble
    assert "hosts" not in s.context_preamble


def test_harness_context_stops_at_the_cap(write_session, tmp_path):
    repo = tmp_path / "repo"
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "big.txt").write_text("y" * (MAX_CONTEXT_CHARS + 10))
    (pristine / "small.txt").write_text("small\n")
    episode = episode_with(write_session, repo, tool_use("Read", file_path="big.txt"),
                           tool_use("Read", file_path="small.txt"))
    context = harness_context(episode, pristine)
    assert "big.txt" not in context and "small.txt" in context


def test_combine_takes_each_causes_fields_from_its_variant():
    combined = combine(BASE, {Cause.TASK: replace(BASE, prompt="clear"), Cause.MODEL: replace(BASE, model=ORACLE_MODEL),
                              Cause.HARNESS: replace(BASE, tools="default", context_preamble="FILES", max_budget_usd=4.0)})
    assert (combined.prompt, combined.model, combined.tools, combined.context_preamble, combined.max_budget_usd) == \
        ("clear", ORACLE_MODEL, "default", "FILES", 4.0)
    assert combined.append_system_prompt is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_oracles.py -v`
Expected: FAIL — `ImportError: cannot import name 'settings_from_json' from 'culprit.replay'`

- [ ] **Step 4: Write the implementation**

Append to `src/culprit/replay.py`:

```python
def settings_to_json(settings: ReplaySettings) -> dict:
    data = asdict(settings)
    data["setup_script"] = str(settings.setup_script) if settings.setup_script else None
    data["disallowed_tools"] = list(settings.disallowed_tools)
    return data


def settings_from_json(data: dict) -> ReplaySettings:
    fields = dict(data)
    fields["setup_script"] = Path(fields["setup_script"]) if fields.get("setup_script") else None
    fields["disallowed_tools"] = tuple(fields.get("disallowed_tools") or ())
    return ReplaySettings(**fields)
```

`src/culprit/oracles.py`:

```python
"""The oracles: for each cause, the baseline with only that cause's fields made as good as they can be.

A variant that passes where the baseline failed proves its cause. Each oracle changes only the fields
VARIANT_FIELDS names for it, and a drafted text runs only after the leak check clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from culprit.claude import Ask, ask_claude
from culprit.criterion import Criterion
from culprit.episodes import Episode, tool_uses
from culprit.leak import check_leak
from culprit.replay import ReplaySettings
from culprit.sandbox import target_inside
from culprit.taxonomy import Cause, Symptom

ORACLE_MODEL = "claude-fable-5-1"
FABLE_PREFIX = "claude-fable"
MAX_CONTEXT_CHARS = 100_000
MAX_DRAFT_ATTEMPTS = 2
HARNESS_BUDGET_MULTIPLIER = 2
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
REWRITE_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

VARIANT_FIELDS: dict[Cause, tuple[str, ...]] = {
    Cause.TASK: ("prompt",),
    Cause.ENVIRONMENT: ("setup_script",),
    Cause.AGENT_SETUP: ("append_system_prompt",),
    Cause.HARNESS: ("tools", "disallowed_tools", "context_preamble", "max_budget_usd"),
    Cause.MODEL: ("model",),
}

SYMPTOM_DESCRIPTIONS = {
    Symptom.STOPPED_SHORT: "the agent stopped before delivering what was asked",
    Symptom.WRONG_RESULT: "the agent delivered a result that was wrong",
    Symptom.IGNORED_INSTRUCTIONS: "the agent broke a rule or instruction it had been given",
    Symptom.WASTEFUL: "the agent reached the result but wasted time, tokens or effort getting there",
}


@dataclass(frozen=True)
class VariantBuild:
    settings: ReplaySettings | None
    untested_reason: str | None


def task_rewrite_prompt(episode: Episode, criterion: Criterion, rejected_evidence: str | None) -> str:
    must_not = "; ".join(criterion.must_not) or "nothing listed"
    retry = (f"\nA previous draft was rejected because it leaked this detail: {rejected_evidence}. Leave it out."
             if rejected_evidence else "")
    return f"""Rewrite one request a person gave a coding agent so that it states its scope and success criterion
clearly, keeping the person's intent and voice.

The original request:
<request>{episode.prompt_text}</request>

What success means, approved by the person: {criterion.success_means}
A correct attempt must not: {must_not}

Add only what the agent could legitimately know before starting: the outcome wanted and what is out of
scope. Do not name the fix, the cause, the file to change, or any detail learned later.{retry}
Return the rewritten request as text."""


def agent_instructions_prompt(criterion: Criterion, symptom: Symptom, rejected_evidence: str | None) -> str:
    retry = (f"\nA previous draft was rejected because it leaked this detail: {rejected_evidence}. Leave it out."
             if rejected_evidence else "")
    return f"""Write standing instructions for a coding agent, the kind a team keeps in CLAUDE.md, that would have
prevented this kind of failure: {SYMPTOM_DESCRIPTIONS[symptom]}.

For context only, success on the current task meant: {criterion.success_means}

The instructions must be general working rules — how to scope, verify and report — true for any task in
this repository. They must not mention this task's specifics, files, values or fix. At most 12 lines.{retry}
Return the instructions as text."""


def harness_context(episode: Episode, pristine: Path) -> str:
    cwd = Path(episode.cwd)
    seen: list[str] = []
    for use in tool_uses(episode):
        if use.get("name") in FILE_TOOLS:
            path = (use.get("input") or {}).get("file_path") or (use.get("input") or {}).get("notebook_path")
            if path and path not in seen:
                seen.append(path)
    blocks: list[str] = []
    used = 0
    shown: set[Path] = set()
    for path in seen:
        target = target_inside(cwd, pristine, path)
        if target is None or target in shown or not target.is_file():
            continue
        try:
            text = target.read_text()
        except UnicodeDecodeError:
            continue  # binary files are not context
        block = f"=== {target.relative_to(pristine.resolve())} ===\n{text}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            continue
        shown.add(target)
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    return "These files from the working directory are relevant to the request below:\n\n" + "".join(blocks)


def build_variant(cause: Cause, base: ReplaySettings, *, episode: Episode, follow_ups: list[str],
                  criterion: Criterion, symptom: Symptom, pristine: Path, env_script: Path | None,
                  ask: Ask = ask_claude) -> VariantBuild:
    if cause is Cause.TASK:
        return _drafted(base, "prompt", lambda evidence: task_rewrite_prompt(episode, criterion, evidence),
                        episode, follow_ups, ask)
    if cause is Cause.AGENT_SETUP:
        return _drafted(base, "append_system_prompt",
                        lambda evidence: agent_instructions_prompt(criterion, symptom, evidence),
                        episode, follow_ups, ask)
    if cause is Cause.ENVIRONMENT:
        if env_script is None:
            return VariantBuild(None, "no environment fix supplied (culprit env-fix)")
        return VariantBuild(replace(base, setup_script=env_script), None)
    if cause is Cause.HARNESS:
        context = harness_context(episode, pristine) or None
        return VariantBuild(replace(base, tools="default", disallowed_tools=(), context_preamble=context,
                                    max_budget_usd=base.max_budget_usd * HARNESS_BUDGET_MULTIPLIER), None)
    if cause is Cause.MODEL:
        if (base.model or "").startswith(FABLE_PREFIX):
            return VariantBuild(None, "the episode already ran on Fable, the strongest model")
        return VariantBuild(replace(base, model=ORACLE_MODEL), None)
    raise ValueError(f"{cause} has no oracle")


def combine(base: ReplaySettings, variants: dict[Cause, ReplaySettings]) -> ReplaySettings:
    changes = {field: getattr(settings, field) for cause, settings in variants.items() for field in VARIANT_FIELDS[cause]}
    return replace(base, **changes)


def _drafted(base: ReplaySettings, field: str, prompt_for, episode: Episode, follow_ups: list[str],
             ask: Ask) -> VariantBuild:
    evidence = None
    for _ in range(MAX_DRAFT_ATTEMPTS):
        text = str(ask(prompt_for(evidence), REWRITE_SCHEMA)["text"]).strip()
        verdict = check_leak(text, episode.prompt_text, follow_ups, ask=ask)
        if not verdict.leaks:
            return VariantBuild(replace(base, **{field: text}), None)
        evidence = verdict.evidence
    return VariantBuild(None, f"the leak check rejected {MAX_DRAFT_ATTEMPTS} drafts (last: {evidence})")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 95 passed.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/sandbox.py src/culprit/replay.py src/culprit/oracles.py tests/test_oracles.py
git commit -m "Build one oracle variant per cause, each changing only its own fields

Task and agent-setup drafts run only after the leak check clears them (two
attempts); environment needs a human script; harness lifts restrictions,
preloads the files the episode touched and doubles the cap; model swaps in
Fable unless the episode already ran on it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 4.4: Decide the cause — first match, pair test, resumable

**Files:**
- Create: `src/culprit/verdict.py`, `src/culprit/diagnose.py`
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `VariantOutcome`, `run_variant`, `load_outcome`, `EpisodeBudget`, `PRISTINE_DIR` (experiment); `ReplaySettings`, `settings_to_json`, `settings_from_json` (replay); `VariantBuild`, `build_variant`, `combine` (oracles); `fisher_one_sided` (stats); `Cause`, `ORACLE_ORDER`, `Symptom` (taxonomy); `episode_dir` (store).
- Produces:
  - In `verdict.py`: `SIGNIFICANCE = 0.05`, `VERDICT_FILE = "verdict.json"`; `beats(variant: VariantOutcome, baseline: VariantOutcome) -> bool`; `reproducible(baseline: VariantOutcome) -> bool`; `@dataclass(frozen=True) class Verdict` with `episode_id: str`, `symptom: str`, `cause: str`, `evidence: str`, `outcomes: dict[str, dict]`, `untested: dict[str, str]`, `pair: tuple[str, ...] = ()`; `save_verdict(folder: Path, verdict: Verdict) -> Path`; `load_verdict(folder: Path) -> Verdict | None`.
  - In `diagnose.py`: `VARIANTS_DIR = "variants"`, `NOT_RUN_REASON: str`, `COMBINED = "combined"`; `diagnose(episode, criterion, symptom, base, *, runs_dir, follow_ups, env_script, n, budget, out_dir=None, ask=ask_claude, run=run_variant, build=build_variant) -> Verdict`.

- [ ] **Step 1: Write the failing tests**

`tests/test_diagnose.py`:

```python
import json
from dataclasses import replace

from culprit.criterion import Criterion
from culprit.diagnose import NOT_RUN_REASON, diagnose
from culprit.episodes import episodes_from_session
from culprit.experiment import EpisodeBudget, VariantOutcome
from culprit.oracles import VariantBuild
from culprit.replay import ReplaySettings
from culprit.store import episode_dir
from culprit.taxonomy import Cause, Symptom
from culprit.verdict import beats, load_verdict, reproducible
from records import human

CRITERION = Criterion("works", (), (), approved=True)
BASE = ReplaySettings(prompt="fix", model="claude-opus-5", permission_mode="auto")


def outcome(name, passes, runs=5):
    return VariantOutcome(name, passes, runs, 1.0)


def scripted_run(passes_by_name):
    calls = []

    def run(name, settings, **kwargs):
        calls.append((name, settings))
        return outcome(name, passes_by_name[name])

    run.calls = calls
    return run


def build_all_tested(untested=()):
    def build(cause, base, **kwargs):
        if cause in untested:
            return VariantBuild(None, f"{cause.value} untested")
        return VariantBuild(replace(base, prompt=f"{cause.value} prompt") if cause is Cause.TASK else
                            replace(base, model=f"{cause.value}-model"), None)
    return build


def run_diagnose(write_session, tmp_path, run, build=None, symptom=Symptom.WRONG_RESULT):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    return diagnose(episode, CRITERION, symptom, BASE, runs_dir=tmp_path / "runs", follow_ups=[], env_script=None,
                    n=5, budget=EpisodeBudget(100.0), run=run, build=build or build_all_tested())


def test_beats_and_reproducible_follow_the_spec():
    assert beats(outcome("task", 4), outcome("baseline", 0)) is True
    assert beats(outcome("task", 3), outcome("baseline", 0)) is False
    assert reproducible(outcome("baseline", 2)) is True
    assert reproducible(outcome("baseline", 3)) is False


def test_a_rejected_candidate_is_misjudged_without_replays(write_session, tmp_path):
    run = scripted_run({})
    verdict = run_diagnose(write_session, tmp_path, run, symptom=Symptom.NO_FAILURE)
    assert verdict.cause == "misjudged"
    assert run.calls == []


def test_a_baseline_that_passes_is_not_reproducible_and_runs_no_oracle(write_session, tmp_path):
    run = scripted_run({"baseline": 3})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "not-reproducible"
    assert [name for name, _ in run.calls] == ["baseline"]


def test_the_first_cause_that_beats_the_baseline_wins_and_later_ones_are_not_run(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 1, "environment": 0, "agent-setup": 5})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "agent-setup"
    assert [name for name, _ in run.calls] == ["baseline", "task", "environment", "agent-setup"]
    assert verdict.untested == {"harness": NOT_RUN_REASON, "model": NOT_RUN_REASON}
    assert "5/5" in verdict.evidence and "0/5" in verdict.evidence
    assert load_verdict(episode_dir(tmp_path / "runs", "s1:p1")) == verdict


def test_untested_causes_are_skipped_and_recorded(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 4})
    verdict = run_diagnose(write_session, tmp_path, run, build=build_all_tested(untested=(Cause.ENVIRONMENT,)))
    assert verdict.cause == "model"
    assert verdict.untested["environment"] == "environment untested"


def test_nothing_alone_but_a_pair_is_multi_cause(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 0,
                        "combined": 5, "task+environment": 0, "task+agent-setup": 4})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert (verdict.cause, verdict.pair) == ("multi-cause", ("task", "agent-setup"))


def test_combined_passes_but_no_pair_is_multi_cause_with_the_full_set(write_session, tmp_path):
    singles = {name: 0 for name in ["task", "environment", "agent-setup", "harness", "model"]}
    run = scripted_run({"baseline": 0, "combined": 5} | singles | {name: 0 for name in [
        "task+environment", "task+agent-setup", "task+harness", "task+model", "environment+agent-setup",
        "environment+harness", "environment+model", "agent-setup+harness", "agent-setup+model", "harness+model"]})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "multi-cause"
    assert verdict.pair == ("task", "environment", "agent-setup", "harness", "model")


def test_nothing_passes_even_combined_is_unexplained(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 0,
                        "combined": 1})
    assert run_diagnose(write_session, tmp_path, run).cause == "unexplained"


def test_one_tested_oracle_that_fails_is_unexplained(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "harness": 0})
    only_harness = build_all_tested(untested=(Cause.TASK, Cause.ENVIRONMENT, Cause.AGENT_SETUP, Cause.MODEL))
    verdict = run_diagnose(write_session, tmp_path, run, build=only_harness)
    assert verdict.cause == "unexplained"
    assert [name for name, _ in run.calls] == ["baseline", "harness"]


def test_a_rerun_reuses_saved_variants_and_outcomes(write_session, tmp_path):
    first = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 5})
    run_diagnose(write_session, tmp_path, first)
    folder = episode_dir(tmp_path / "runs", "s1:p1")
    for name in ["baseline", "task", "environment", "agent-setup", "harness", "model"]:
        (folder / "replays" / name).mkdir(parents=True, exist_ok=True)
        (folder / "replays" / name / "outcome.json").write_text(
            json.dumps({"variant": name, "passes": 5 if name == "model" else 0, "runs": 5, "cost_usd": 1.0}))
    builds = []

    def counting_build(cause, base, **kwargs):
        builds.append(cause)
        return build_all_tested()(cause, base)

    second = scripted_run({})
    verdict = run_diagnose(write_session, tmp_path, second, build=counting_build)
    assert verdict.cause == "model"
    assert second.calls == [] and builds == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_diagnose.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.diagnose'`

- [ ] **Step 3: Write the implementation**

`src/culprit/verdict.py`:

```python
"""The decision rules and the stored verdict: one cause per episode, with the pass rates that prove it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from culprit.experiment import VariantOutcome
from culprit.stats import fisher_one_sided

SIGNIFICANCE = 0.05
VERDICT_FILE = "verdict.json"


@dataclass(frozen=True)
class Verdict:
    episode_id: str
    symptom: str
    cause: str
    evidence: str
    outcomes: dict[str, dict]
    untested: dict[str, str]
    pair: tuple[str, ...] = ()


def beats(variant: VariantOutcome, baseline: VariantOutcome) -> bool:
    return fisher_one_sided(variant.passes, variant.runs, baseline.passes, baseline.runs) < SIGNIFICANCE


def reproducible(baseline: VariantOutcome) -> bool:
    """The failure reproduces unless the unchanged replay passes most of the time."""
    return baseline.passes * 2 <= baseline.runs


def save_verdict(folder: Path, verdict: Verdict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / VERDICT_FILE
    path.write_text(json.dumps(asdict(verdict), indent=2))
    return path


def load_verdict(folder: Path) -> Verdict | None:
    path = folder / VERDICT_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data["pair"] = tuple(data.get("pair") or ())
    return Verdict(**data)
```

`src/culprit/diagnose.py`:

```python
"""Diagnose one episode: baseline, then each oracle in cause order until one beats the baseline,
then the pair test. Every built variant and every outcome is saved, so a run stopped by the budget
resumes where it stopped instead of paying again.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from itertools import combinations
from pathlib import Path

from culprit.claude import ask_claude
from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.experiment import PRISTINE_DIR, EpisodeBudget, VariantOutcome, load_outcome, run_variant
from culprit.oracles import build_variant, combine
from culprit.replay import ReplaySettings, settings_from_json, settings_to_json
from culprit.store import episode_dir
from culprit.taxonomy import ORACLE_ORDER, Cause, Symptom
from culprit.verdict import Verdict, beats, reproducible, save_verdict

VARIANTS_DIR = "variants"
BASELINE = "baseline"
COMBINED = "combined"
NOT_RUN_REASON = "not run: an earlier cause already explained the failure"


def diagnose(episode: Episode, criterion: Criterion, symptom: Symptom, base: ReplaySettings, *, runs_dir: Path,
             follow_ups: list[str], env_script: Path | None, n: int, budget: EpisodeBudget,
             out_dir: Path | None = None, ask=ask_claude, run=run_variant, build=build_variant) -> Verdict:
    folder = out_dir or episode_dir(runs_dir, episode.episode_id)
    pristine = episode_dir(runs_dir, episode.episode_id) / PRISTINE_DIR
    outcomes: dict[str, VariantOutcome] = {}
    untested: dict[str, str] = {}

    def measured(name: str, settings: ReplaySettings) -> VariantOutcome:
        saved = load_outcome(folder, name)
        result = saved or run(name, settings, n=n, episode=episode, runs_dir=runs_dir, criterion=criterion,
                              budget=budget, out_dir=out_dir)
        outcomes[name] = result
        return result

    def finish(cause: Cause, evidence: str, pair: tuple[str, ...] = ()) -> Verdict:
        verdict = Verdict(episode.episode_id, symptom.value, cause.value, evidence,
                          {name: asdict(o) for name, o in outcomes.items()}, untested, pair)
        save_verdict(folder, verdict)
        return verdict

    if symptom is Symptom.NO_FAILURE:
        return finish(Cause.MISJUDGED, "the human confirmed this candidate was not a failure")

    baseline = measured(BASELINE, base)
    if not reproducible(baseline):
        return finish(Cause.NOT_REPRODUCIBLE, f"the unchanged replay passed {_rate(baseline)}")

    built: dict[Cause, ReplaySettings] = {}
    for position, cause in enumerate(ORACLE_ORDER):
        settings = _oracle(folder, cause, base, untested, lambda c=cause: build(
            c, base, episode=episode, follow_ups=follow_ups, criterion=criterion, symptom=symptom,
            pristine=pristine, env_script=env_script, ask=ask))
        if settings is None:
            continue
        built[cause] = settings
        result = measured(cause.value, settings)
        if beats(result, baseline):
            for later in ORACLE_ORDER[position + 1:]:
                untested.setdefault(later.value, NOT_RUN_REASON)
            return finish(cause, f"{cause.value} passed {_rate(result)} against the baseline's {_rate(baseline)}")

    if len(built) < 2:
        return finish(Cause.UNEXPLAINED, "no single change made it pass, and fewer than two changes could be tested")
    together = measured(COMBINED, combine(base, built))
    if not beats(together, baseline):
        return finish(Cause.UNEXPLAINED, f"even every change together passed only {_rate(together)}")
    for first, second in combinations(list(built), 2):
        name = f"{first.value}+{second.value}"
        result = measured(name, combine(base, {first: built[first], second: built[second]}))
        if beats(result, baseline):
            return finish(Cause.MULTI_CAUSE, f"{name} passed {_rate(result)} against the baseline's {_rate(baseline)}",
                          (first.value, second.value))
    return finish(Cause.MULTI_CAUSE, f"only every change together passed ({_rate(together)})",
                  tuple(c.value for c in built))


def _oracle(folder: Path, cause: Cause, base: ReplaySettings, untested: dict[str, str], make) -> ReplaySettings | None:
    path = folder / VARIANTS_DIR / f"{cause.value}.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        made = make()
        data = {"untested": made.untested_reason} if made.settings is None else settings_to_json(made.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    if "untested" in data:
        untested[cause.value] = data["untested"]
        return None
    return settings_from_json(data)


def _rate(outcome: VariantOutcome) -> str:
    return f"{outcome.passes}/{outcome.runs}"
```

Note on the resume test: the first run saved every variant's settings under `variants/`, and the test then writes every outcome, so the second run neither builds nor replays anything and reaches the same verdict.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 105 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/verdict.py src/culprit/diagnose.py tests/test_diagnose.py
git commit -m "Decide each episode's cause: first oracle to beat the baseline, then the pair test

Misjudged needs no replay; a mostly passing baseline is Not reproducible;
later oracles are not run once one wins; combined-then-pairs separates
Multi-cause from Unexplained. Built variants and outcomes are saved, so a
budget stop resumes without paying twice.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 4.5: `culprit env-fix` and `culprit diagnose`, proven on a real episode

**Files:**
- Modify: `src/culprit/cli.py`
- Test: `tests/test_cli_stage4.py`

**Interfaces:**
- Consumes: Stage 3's CLI (`ready_to_replay`); `read_symptom` (store); `follow_up_prompts` (locate); `baseline_settings` (replay); `EpisodeBudget` (experiment); `diagnose` (diagnose).
- Produces: subcommands `env-fix <episode_id> <script>` (copies the script to `runs/<episode>/env-fix.sh`) and `diagnose <episode_id> [--n 5] [--budget 40] [--replay-budget 2.0]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_stage4.py`:

```python
import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.store import episode_dir
from culprit.verdict import Verdict
from records import human, snapshot


def ready(write_session, tmp_path, confirm=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo)),
                   human("still wrong", "p2", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    if confirm:
        (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wrong-result"}')
    return tmp_path / "projects", runs


def test_env_fix_stores_the_script_for_the_environment_oracle(write_session, tmp_path):
    projects, runs = ready(write_session, tmp_path)
    script = tmp_path / "fix.sh"
    script.write_text("export FLAKY=0\n")
    assert cli.main(["env-fix", "s1:p1", str(script), "--runs", str(runs)]) == 0
    assert (episode_dir(runs, "s1:p1") / "env-fix.sh").read_text() == "export FLAKY=0\n"


def test_diagnose_passes_everything_through_and_prints_the_verdict(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = ready(write_session, tmp_path)
    seen = {}

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        seen.update(symptom=symptom.value, follow_ups=kwargs["follow_ups"], cap=kwargs["budget"].cap_usd,
                    env=kwargs["env_script"], n=kwargs["n"])
        return Verdict("s1:p1", "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5",
                       {}, {"environment": "no environment fix supplied (culprit env-fix)"})

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    assert cli.main(["diagnose", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    assert seen == {"symptom": "wrong-result", "follow_ups": ["still wrong"], "cap": 40.0, "env": None, "n": 5}
    assert "cause: harness" in out and "4/5" in out and "environment untested" in out


def test_diagnose_before_confirm_names_confirm(write_session, tmp_path, capsys):
    projects, runs = ready(write_session, tmp_path, confirm=False)
    assert cli.main(["diagnose", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 1
    assert "culprit confirm" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_stage4.py -v`
Expected: FAIL — `invalid choice: 'env-fix'` (SystemExit 2).

- [ ] **Step 3: Add the subcommands**

In `src/culprit/cli.py`, add to the imports:

```python
import shutil

from culprit.diagnose import diagnose
from culprit.store import read_symptom
```

Add before `return parser` in `build_parser`:

```python
    env_fix = commands.add_parser("env-fix", help="supply a script that fixes the environment for the environment oracle")
    env_fix.add_argument("episode_id")
    env_fix.add_argument("script", type=Path)
    env_fix.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    env_fix.set_defaults(handler=env_fix_command)

    diagnose_parser = commands.add_parser("diagnose", help="find what to change: run the oracles and decide the cause")
    diagnose_parser.add_argument("episode_id")
    _projects_and_runs(diagnose_parser)
    diagnose_parser.add_argument("--n", type=int, default=5)
    diagnose_parser.add_argument("--budget", type=float, default=40.0, help="spend cap in USD for this episode")
    diagnose_parser.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    diagnose_parser.set_defaults(handler=diagnose_command)
```

Add at the end of the file:

```python
ENV_FIX_FILE = "env-fix.sh"


def env_fix_command(args: argparse.Namespace) -> int:
    destination = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.script, destination)
    print(f"stored {destination}; the environment oracle runs it in each replay's clone before Claude starts")
    return 0


def diagnose_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    symptom = read_symptom(args.runs, args.episode_id)
    if symptom is None:
        raise FileNotFoundError(f"{args.episode_id} has no confirmed symptom — run `culprit confirm {args.episode_id} <symptom>` first")
    env_script = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    verdict = diagnose(episode, criterion, symptom, baseline_settings(episode, args.replay_budget),
                       runs_dir=args.runs, follow_ups=follow_up_prompts(args.projects, args.episode_id),
                       env_script=env_script if env_script.exists() else None, n=args.n,
                       budget=EpisodeBudget(args.budget))
    print(f"cause: {verdict.cause}\nevidence: {verdict.evidence}")
    if verdict.pair:
        print(f"together: {' + '.join(verdict.pair)}")
    for cause, reason in verdict.untested.items():
        print(f"{cause} untested: {reason}")
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 108 passed.

- [ ] **Step 5: Prove it on a real episode**

Use an episode whose Stage 3 baseline passed at most 2 of 5 (a reproducible failure). Its saved baseline (out of 5) is reused. Start with N = 2 for the oracles to prove the wiring for little money:

```bash
uv run culprit diagnose <ID> --n 2 --budget 15
```

Expected: a `cause:` line, an `evidence:` line naming two pass rates, and one `untested:` line per skipped cause. Read what it actually wrote:

```bash
F=runs/<ID with : as __>
ls $F/variants $F/replays
cat $F/variants/task.json $F/variants/agent-setup.json     # drafted texts: read them for leaks yourself
cat $F/verdict.json
```

Expected: the task and agent-setup texts contain no detail from your follow-up messages beyond the outcome you wanted. If one does, the leak check missed it: record it, tighten `leak_prompt`, delete that variant file, and rerun.

Then the real diagnosis. Delete the N = 2 outcomes (their pass counts are out of 2, not 5) but keep the drafted variants, so the same texts are measured:

```bash
rm -r $F/replays/{task,environment,agent-setup,harness,model,combined} 2>/dev/null; rm -f $F/verdict.json
uv run culprit diagnose <ID>
```

Expected: a verdict whose evidence quotes rates out of 5. Record the episode, cause, evidence, total spend (sum of `cost_usd` in `$F/replays/*/outcome.json`) and wall time in the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/cli.py tests/test_cli_stage4.py
git commit -m "Add culprit env-fix and culprit diagnose

Proven on <ID>: cause <cause> — <evidence>; \$<spend> over <wall time>.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

## Stage 5 — Judge calibration and selftest

**Goal:** culprit measures how far its own verdicts can be trusted. `culprit review` / `label` / `calibration` compare the judge with the human on real replays (Cohen's kappa); `culprit selftest` plants one known fault per layer into a control episode and reports how often `diagnose` names it. Both figures are printed beside every verdict.

**Architecture:** `calibration` owns human labels (one `human_label.json` beside each `judgement.json`) and the agreement statistic. `selftest` owns the planted faults, the cause each one should produce, the control-episode rule, and the accuracy over stored selftest verdicts; it calls Stage 4's `diagnose` with an `out_dir` under `runs/selftest/`, so planted runs never mix with real ones.

**Spec:** §4 guards 2, 4 and 5.

### Global Constraints

- Calibrated means: at least 20 human-labelled replays **and** Cohen's kappa ≥ 0.6. Until then every verdict prints `uncalibrated`.
- A control episode is one whose saved baseline passed at least 4 of 5.
- Planted faults and their expected causes, exactly: `truncated-prompt` → task (prompt cut to its first 6 words); `misleading-instruction` → agent-setup (appended: "Do not modify any files. Reply with a one-line summary of what you would do, then stop."); `edit-tools-removed` → harness (`tools="Read,Grep,Glob"`); `weak-model` → model (`claude-haiku-5-5`); `impossible-criterion` → unexplained (a check that always fails).
- Selftest verdicts live in `runs/selftest/<episode folder>/<fault>/verdict.json`; real calibration counts only runs under `runs/<episode folder>/replays/`.

### Review Focus

1. **Kappa is undefined when every label agrees on one value** (all pass, say): report "not computable yet", never divide by zero or print a fake 1.0. Pinned in Task 5.1 (`test_kappa_is_undefined_when_one_value_is_certain`).
2. **Labelling a run id that does not exist** must refuse and write nothing. Pinned in Task 5.1 (`test_labelling_an_unknown_run_is_refused`).
3. **Selftest on an episode that is not a control** must refuse before spending anything. Pinned in Task 5.3 (`test_selftest_refuses_an_episode_that_is_not_a_control`).
4. **An expected-versus-got mismatch must be visible per fault**, not only as one accuracy number. Pinned in Task 5.2 (`test_selftest_reports_each_fault_and_the_accuracy`).

### File Structure

```
src/culprit/
├── calibration.py   # record_label, cohens_kappa, Agreement, agreement, next_unlabelled, run_summary
├── selftest.py      # PlantedFault, EXPECTED, plant, is_control, run_selftest, selftest_accuracy
└── cli.py           # MODIFY: add review, label, calibration, selftest; diagnose prints trust figures
tests/
├── test_calibration.py
├── test_selftest.py
└── test_cli_stage5.py
```

---

#### Task 5.1: Human labels and the judge's agreement with them

**Files:**
- Create: `src/culprit/calibration.py`
- Test: `tests/test_calibration.py`

**Interfaces:**
- Consumes: `JUDGEMENT_FILE`, `REPLAYS_DIR` (experiment).
- Produces:
  - `HUMAN_LABEL_FILE = "human_label.json"`, `MIN_LABELS = 20`, `MIN_KAPPA = 0.6`
  - `class UnknownRun(LookupError)`
  - `judged_runs(runs_dir: Path) -> list[Path]` — every `runs/<episode>/replays/<variant>/<i>/` holding a `judgement.json`, sorted.
  - `run_id(runs_dir: Path, run_dir: Path) -> str` — the run's path relative to `runs_dir`.
  - `record_label(runs_dir: Path, run: str, passed: bool) -> Path`
  - `cohens_kappa(pairs: list[tuple[bool, bool]]) -> float | None` — pairs are (judge, human).
  - `@dataclass(frozen=True) class Agreement` with `labelled: int`, `agreed: int`, `kappa: float | None`, and property `calibrated -> bool`.
  - `agreement(runs_dir: Path) -> Agreement`
  - `next_unlabelled(runs_dir: Path) -> Path | None`
  - `run_summary(run_dir: Path) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_calibration.py`:

```python
import json

import pytest

from culprit.calibration import (Agreement, UnknownRun, agreement, cohens_kappa, judged_runs, next_unlabelled,
                                 record_label, run_id, run_summary)


def judged(runs, episode, variant, i, passed, reason="r"):
    run_dir = runs / episode / "replays" / variant / str(i)
    run_dir.mkdir(parents=True)
    (run_dir / "judgement.json").write_text(json.dumps({"passed": passed, "reason": reason, "by": "model"}))
    (run_dir / "result.json").write_text(json.dumps({"result_text": "I fixed it", "cost_usd": 0.2}))
    (run_dir / "diff.patch").write_text("--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n")
    return run_dir


def test_kappa_matches_the_textbook_formula():
    pairs = [(True, True), (True, True), (False, False), (True, False)]
    assert cohens_kappa(pairs) == pytest.approx(0.5)


def test_kappa_is_undefined_when_one_value_is_certain():
    assert cohens_kappa([(True, True), (True, True)]) is None
    assert cohens_kappa([]) is None


def test_only_real_episode_runs_count_and_selftest_runs_do_not(tmp_path):
    runs = tmp_path / "runs"
    real = judged(runs, "s1__p1", "baseline", 0, True)
    judged(runs / "selftest" / "s1__p1", "weak-model", "baseline", 0, True)
    assert judged_runs(runs) == [real]
    assert run_id(runs, real) == "s1__p1/replays/baseline/0"


def test_labels_drive_agreement_and_the_calibration_gate(tmp_path):
    runs = tmp_path / "runs"
    for i in range(10):
        judged(runs, "s1__p1", "baseline", i, passed=i < 5)
        judged(runs, "s2__p1", "task", i, passed=i < 5)
    for run_dir in judged_runs(runs):
        judge_passed = json.loads((run_dir / "judgement.json").read_text())["passed"]
        record_label(runs, run_id(runs, run_dir), judge_passed)
    result = agreement(runs)
    assert result == Agreement(labelled=20, agreed=20, kappa=1.0)
    assert result.calibrated is True
    assert Agreement(labelled=19, agreed=19, kappa=1.0).calibrated is False
    assert Agreement(labelled=30, agreed=20, kappa=0.4).calibrated is False
    assert Agreement(labelled=30, agreed=30, kappa=None).calibrated is False


def test_labelling_an_unknown_run_is_refused(tmp_path):
    with pytest.raises(UnknownRun, match="s9__p9/replays/baseline/0"):
        record_label(tmp_path, "s9__p9/replays/baseline/0", True)
    assert not (tmp_path / "s9__p9").exists()


def test_next_unlabelled_walks_the_runs_in_order(tmp_path):
    runs = tmp_path / "runs"
    first = judged(runs, "s1__p1", "baseline", 0, True)
    second = judged(runs, "s1__p1", "baseline", 1, False)
    assert next_unlabelled(runs) == first
    record_label(runs, run_id(runs, first), True)
    assert next_unlabelled(runs) == second
    record_label(runs, run_id(runs, second), False)
    assert next_unlabelled(runs) is None


def test_the_summary_shows_answer_judgement_and_diff(tmp_path):
    summary = run_summary(judged(tmp_path, "s1__p1", "baseline", 0, False, reason="tests still fail"))
    for expected in ["I fixed it", "judge: fail — tests still fail", "+new", "1 file changed"]:
        assert expected in summary
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_calibration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.calibration'`

- [ ] **Step 3: Write the implementation**

`src/culprit/calibration.py`:

```python
"""How far the judge can be trusted: the human labels real replays and Cohen's kappa measures agreement.

Verdicts are marked uncalibrated until at least MIN_LABELS replays are labelled and kappa reaches MIN_KAPPA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from culprit.experiment import JUDGEMENT_FILE, REPLAYS_DIR

HUMAN_LABEL_FILE = "human_label.json"
MIN_LABELS = 20
MIN_KAPPA = 0.6
DIFF_PREVIEW_LINES = 40


class UnknownRun(LookupError):
    pass


@dataclass(frozen=True)
class Agreement:
    labelled: int
    agreed: int
    kappa: float | None

    @property
    def calibrated(self) -> bool:
        return self.labelled >= MIN_LABELS and self.kappa is not None and self.kappa >= MIN_KAPPA


def judged_runs(runs_dir: Path) -> list[Path]:
    return sorted(path.parent for path in runs_dir.glob(f"*/{REPLAYS_DIR}/*/*/{JUDGEMENT_FILE}"))


def run_id(runs_dir: Path, run_dir: Path) -> str:
    return str(run_dir.relative_to(runs_dir))


def record_label(runs_dir: Path, run: str, passed: bool) -> Path:
    run_dir = runs_dir / run
    if not (run_dir / JUDGEMENT_FILE).is_file():
        raise UnknownRun(f"no judged replay at {run} under {runs_dir}")
    path = run_dir / HUMAN_LABEL_FILE
    path.write_text(json.dumps({"passed": passed}))
    return path


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(judge == human for judge, human in pairs) / n
    judge_yes = sum(judge for judge, _ in pairs) / n
    human_yes = sum(human for _, human in pairs) / n
    expected = judge_yes * human_yes + (1 - judge_yes) * (1 - human_yes)
    if expected == 1:
        return None  # both raters gave one value every time: agreement beyond chance is not measurable
    return (observed - expected) / (1 - expected)


def agreement(runs_dir: Path) -> Agreement:
    pairs = []
    for run_dir in judged_runs(runs_dir):
        label = run_dir / HUMAN_LABEL_FILE
        if label.is_file():
            judge = json.loads((run_dir / JUDGEMENT_FILE).read_text())["passed"]
            pairs.append((bool(judge), bool(json.loads(label.read_text())["passed"])))
    return Agreement(len(pairs), sum(j == h for j, h in pairs), cohens_kappa(pairs))


def next_unlabelled(runs_dir: Path) -> Path | None:
    return next((run_dir for run_dir in judged_runs(runs_dir) if not (run_dir / HUMAN_LABEL_FILE).exists()), None)


def run_summary(run_dir: Path) -> str:
    judgement = json.loads((run_dir / JUDGEMENT_FILE).read_text())
    result = json.loads((run_dir / "result.json").read_text())
    diff_lines = (run_dir / "diff.patch").read_text(errors="replace").splitlines()
    files_changed = sum(1 for line in diff_lines if line.startswith("+++ "))
    verdict = "pass" if judgement["passed"] else "fail"
    preview = "\n".join(diff_lines[:DIFF_PREVIEW_LINES])
    more = f"\n… {len(diff_lines) - DIFF_PREVIEW_LINES} more diff lines" if len(diff_lines) > DIFF_PREVIEW_LINES else ""
    return (f"final answer: {result.get('result_text', '')}\n"
            f"judge: {verdict} — {judgement['reason']}\n"
            f"{files_changed} file{'s' if files_changed != 1 else ''} changed\n{preview}{more}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 115 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/calibration.py tests/test_calibration.py
git commit -m "Measure the judge against human labels with Cohen's kappa and a calibration gate

Calibrated needs at least 20 labelled real replays and kappa >= 0.6; kappa
is reported as not computable when every label has one value.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 5.2: Plant known faults and measure culprit's accuracy

**Files:**
- Create: `src/culprit/selftest.py`
- Test: `tests/test_selftest.py`

**Interfaces:**
- Consumes: `Criterion` (criterion); `ReplaySettings` (replay); `EpisodeBudget`, `load_outcome` (experiment); `diagnose` (diagnose); `Verdict`, `load_verdict` (verdict); `Cause`, `Symptom` (taxonomy); `episode_dir` (store).
- Produces:
  - `class PlantedFault(StrEnum)`: `TRUNCATED_PROMPT = "truncated-prompt"`, `MISLEADING_INSTRUCTION = "misleading-instruction"`, `EDIT_TOOLS_REMOVED = "edit-tools-removed"`, `WEAK_MODEL = "weak-model"`, `IMPOSSIBLE_CRITERION = "impossible-criterion"`.
  - `EXPECTED: dict[PlantedFault, Cause]`, `SELFTEST_DIR = "selftest"`, `CONTROL_MIN_PASSES = 4`, `WEAK_MODEL_ID = "claude-haiku-5-5"`, `TRUNCATED_WORDS = 6`, `MISLEADING_INSTRUCTION_TEXT: str`, `READ_ONLY_TOOLS = "Read,Grep,Glob"`, `IMPOSSIBLE: Criterion`.
  - `plant(fault: PlantedFault, base: ReplaySettings, criterion: Criterion) -> tuple[ReplaySettings, Criterion]`
  - `is_control(runs_dir: Path, episode_id: str) -> bool`
  - `selftest_dir(runs_dir: Path, episode_id: str, fault: PlantedFault) -> Path`
  - `@dataclass(frozen=True) class SelftestResult` with `fault: str`, `expected: str`, `got: str`, and property `correct -> bool`.
  - `run_selftest(episode, criterion, base, *, runs_dir, follow_ups, n, budget_per_fault, faults=tuple(PlantedFault), diagnose_fn=diagnose) -> list[SelftestResult]`
  - `selftest_results(runs_dir: Path) -> list[SelftestResult]` and `selftest_accuracy(runs_dir: Path) -> tuple[int, int]` — (correct, total) over every stored selftest verdict.

- [ ] **Step 1: Write the failing tests**

`tests/test_selftest.py`:

```python
import json
from dataclasses import replace

from culprit.criterion import Criterion
from culprit.episodes import episodes_from_session
from culprit.replay import ReplaySettings
from culprit.selftest import (EXPECTED, IMPOSSIBLE, MISLEADING_INSTRUCTION_TEXT, PlantedFault, SelftestResult,
                              is_control, plant, run_selftest, selftest_accuracy, selftest_dir)
from culprit.store import episode_dir
from culprit.taxonomy import Cause
from culprit.verdict import Verdict, save_verdict
from records import human

CRITERION = Criterion("works", ("pytest -q",), (), approved=True)
BASE = ReplaySettings(prompt="fix the failing login test in the auth module please", model="claude-opus-5",
                      permission_mode="auto")


def test_each_fault_breaks_exactly_one_layer():
    assert plant(PlantedFault.TRUNCATED_PROMPT, BASE, CRITERION) == (
        replace(BASE, prompt="fix the failing login test in"), CRITERION)
    assert plant(PlantedFault.MISLEADING_INSTRUCTION, BASE, CRITERION) == (
        replace(BASE, append_system_prompt=MISLEADING_INSTRUCTION_TEXT), CRITERION)
    assert plant(PlantedFault.EDIT_TOOLS_REMOVED, BASE, CRITERION) == (replace(BASE, tools="Read,Grep,Glob"), CRITERION)
    assert plant(PlantedFault.WEAK_MODEL, BASE, CRITERION) == (replace(BASE, model="claude-haiku-5-5"), CRITERION)
    assert plant(PlantedFault.IMPOSSIBLE_CRITERION, BASE, CRITERION) == (BASE, IMPOSSIBLE)


def test_every_fault_has_its_expected_cause():
    assert {f.value: c.value for f, c in EXPECTED.items()} == {
        "truncated-prompt": "task", "misleading-instruction": "agent-setup", "edit-tools-removed": "harness",
        "weak-model": "model", "impossible-criterion": "unexplained"}
    assert IMPOSSIBLE.checks and all("false" in check for check in IMPOSSIBLE.checks)


def test_a_control_needs_four_of_five_baseline_passes(tmp_path):
    folder = episode_dir(tmp_path, "s1:p1") / "replays" / "baseline"
    folder.mkdir(parents=True)
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 3, "runs": 5, "cost_usd": 1}))
    assert is_control(tmp_path, "s1:p1") is False
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 4, "runs": 5, "cost_usd": 1}))
    assert is_control(tmp_path, "s1:p1") is True
    assert is_control(tmp_path, "s9:p9") is False


def test_selftest_reports_each_fault_and_the_accuracy(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    runs = tmp_path / "runs"
    calls = []
    answers = {"truncated-prompt": "task", "misleading-instruction": "agent-setup", "edit-tools-removed": "harness",
               "weak-model": "task", "impossible-criterion": "unexplained"}

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        fault = kwargs["out_dir"].name
        calls.append((fault, base, criterion, kwargs["budget"].cap_usd))
        verdict = Verdict(episode.episode_id, symptom.value, answers[fault], "e", {}, {})
        save_verdict(kwargs["out_dir"], verdict)
        return verdict

    results = run_selftest(episode, CRITERION, BASE, runs_dir=runs, follow_ups=[], n=5, budget_per_fault=30.0,
                           diagnose_fn=fake_diagnose)
    assert [r.fault for r in results] == [f.value for f in PlantedFault]
    assert [r.correct for r in results] == [True, True, True, False, True]
    assert SelftestResult("weak-model", "model", "task").correct is False
    assert all(cap == 30.0 for *_, cap in calls)
    assert calls[3][1].model == "claude-haiku-5-5"
    assert selftest_dir(runs, "s1:p1", PlantedFault.WEAK_MODEL) == runs / "selftest" / "s1__p1" / "weak-model"
    assert selftest_accuracy(runs) == (4, 5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_selftest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'culprit.selftest'`

- [ ] **Step 3: Write the implementation**

`src/culprit/selftest.py`:

```python
"""culprit tested on itself: break one layer of a control episode on purpose and check diagnose names it.

A control episode is one whose unchanged replay passes at least 4 of 5, so any new failure comes from
the planted fault. The share of faults named correctly is culprit's accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from culprit.criterion import Criterion
from culprit.diagnose import diagnose
from culprit.episodes import Episode
from culprit.experiment import EpisodeBudget, load_outcome
from culprit.replay import ReplaySettings
from culprit.store import episode_dir
from culprit.taxonomy import Cause, Symptom
from culprit.verdict import load_verdict

SELFTEST_DIR = "selftest"
CONTROL_MIN_PASSES = 4
WEAK_MODEL_ID = "claude-haiku-5-5"
TRUNCATED_WORDS = 6
READ_ONLY_TOOLS = "Read,Grep,Glob"
MISLEADING_INSTRUCTION_TEXT = "Do not modify any files. Reply with a one-line summary of what you would do, then stop."
IMPOSSIBLE = Criterion(
    success_means="a file named CULPRIT_IMPOSSIBLE_MARKER exists and the check below passes",
    checks=("test -f CULPRIT_IMPOSSIBLE_MARKER && false",),  # can never exit 0: every run fails
    must_not=(),
    approved=True,
)


class PlantedFault(StrEnum):
    TRUNCATED_PROMPT = "truncated-prompt"
    MISLEADING_INSTRUCTION = "misleading-instruction"
    EDIT_TOOLS_REMOVED = "edit-tools-removed"
    WEAK_MODEL = "weak-model"
    IMPOSSIBLE_CRITERION = "impossible-criterion"


EXPECTED: dict[PlantedFault, Cause] = {
    PlantedFault.TRUNCATED_PROMPT: Cause.TASK,
    PlantedFault.MISLEADING_INSTRUCTION: Cause.AGENT_SETUP,
    PlantedFault.EDIT_TOOLS_REMOVED: Cause.HARNESS,
    PlantedFault.WEAK_MODEL: Cause.MODEL,
    PlantedFault.IMPOSSIBLE_CRITERION: Cause.UNEXPLAINED,
}


@dataclass(frozen=True)
class SelftestResult:
    fault: str
    expected: str
    got: str

    @property
    def correct(self) -> bool:
        return self.expected == self.got


def plant(fault: PlantedFault, base: ReplaySettings, criterion: Criterion) -> tuple[ReplaySettings, Criterion]:
    if fault is PlantedFault.TRUNCATED_PROMPT:
        return replace(base, prompt=" ".join(base.prompt.split()[:TRUNCATED_WORDS])), criterion
    if fault is PlantedFault.MISLEADING_INSTRUCTION:
        return replace(base, append_system_prompt=MISLEADING_INSTRUCTION_TEXT), criterion
    if fault is PlantedFault.EDIT_TOOLS_REMOVED:
        return replace(base, tools=READ_ONLY_TOOLS), criterion
    if fault is PlantedFault.WEAK_MODEL:
        return replace(base, model=WEAK_MODEL_ID), criterion
    return base, IMPOSSIBLE


def is_control(runs_dir: Path, episode_id: str) -> bool:
    baseline = load_outcome(episode_dir(runs_dir, episode_id), "baseline")
    return baseline is not None and baseline.passes >= CONTROL_MIN_PASSES


def selftest_dir(runs_dir: Path, episode_id: str, fault: PlantedFault) -> Path:
    return runs_dir / SELFTEST_DIR / episode_dir(runs_dir, episode_id).name / fault.value


def run_selftest(episode: Episode, criterion: Criterion, base: ReplaySettings, *, runs_dir: Path,
                 follow_ups: list[str], n: int, budget_per_fault: float,
                 faults: tuple[PlantedFault, ...] = tuple(PlantedFault), diagnose_fn=diagnose) -> list[SelftestResult]:
    results = []
    for fault in faults:
        planted_base, planted_criterion = plant(fault, base, criterion)
        verdict = diagnose_fn(episode, planted_criterion, Symptom.WRONG_RESULT, planted_base, runs_dir=runs_dir,
                              follow_ups=follow_ups, env_script=None, n=n,
                              budget=EpisodeBudget(budget_per_fault),
                              out_dir=selftest_dir(runs_dir, episode.episode_id, fault))
        results.append(SelftestResult(fault.value, EXPECTED[fault].value, verdict.cause))
    return results


def selftest_results(runs_dir: Path) -> list[SelftestResult]:
    results = []
    for fault_dir in sorted((runs_dir / SELFTEST_DIR).glob("*/*")):
        verdict = load_verdict(fault_dir)
        if verdict is not None and fault_dir.name in {f.value for f in PlantedFault}:
            results.append(SelftestResult(fault_dir.name, EXPECTED[PlantedFault(fault_dir.name)].value, verdict.cause))
    return results


def selftest_accuracy(runs_dir: Path) -> tuple[int, int]:
    results = selftest_results(runs_dir)
    return sum(r.correct for r in results), len(results)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 119 passed.

- [ ] **Step 5: Commit**

```bash
git add src/culprit/selftest.py tests/test_selftest.py
git commit -m "Plant one known fault per layer into a control episode and measure culprit's accuracy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```

---

#### Task 5.3: `culprit review`, `label`, `calibration`, `selftest`; trust figures beside every verdict

**Files:**
- Modify: `src/culprit/cli.py`
- Test: `tests/test_cli_stage5.py`

**Interfaces:**
- Consumes: Stage 4's CLI; `calibration` and `selftest` modules.
- Produces: subcommands `review`, `label <run-id> pass|fail`, `calibration`, `selftest <episode_id> [--n 5] [--budget-per-fault 40] [--replay-budget 2.0]`; `trust_lines(runs_dir: Path) -> list[str]`, printed after every `diagnose` verdict.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_stage5.py`:

```python
import json

import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.selftest import SelftestResult
from culprit.store import episode_dir
from records import human, snapshot


def judged(runs, i, passed):
    run_dir = runs / "s1__p1" / "replays" / "baseline" / str(i)
    run_dir.mkdir(parents=True)
    (run_dir / "judgement.json").write_text(json.dumps({"passed": passed, "reason": "r", "by": "model"}))
    (run_dir / "result.json").write_text(json.dumps({"result_text": "done"}))
    (run_dir / "diff.patch").write_text("")
    return run_dir


def test_review_then_label_then_calibration(tmp_path, capsys):
    runs = tmp_path / "runs"
    judged(runs, 0, True)
    assert cli.main(["review", "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "s1__p1/replays/baseline/0" in out and "judge: pass" in out
    assert cli.main(["label", "s1__p1/replays/baseline/0", "pass", "--runs", str(runs)]) == 0
    assert cli.main(["review", "--runs", str(runs)]) == 0
    assert "every judged replay is labelled" in capsys.readouterr().out
    assert cli.main(["calibration", "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "1 labelled" in out and "uncalibrated" in out


def test_trust_lines_report_calibration_and_selftest(tmp_path):
    lines = cli.trust_lines(tmp_path / "runs")
    assert any("uncalibrated" in line for line in lines)
    assert any("selftest: not run" in line for line in lines)


def test_selftest_refuses_an_episode_that_is_not_a_control(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    def must_not_run(*args, **kwargs):
        raise AssertionError("selftest spent money on an episode that is not a control")

    monkeypatch.setattr(cli, "run_selftest", must_not_run)
    assert cli.main(["selftest", "s1:p1", "--projects", str(tmp_path / "projects"), "--runs", str(runs)]) == 1
    assert "control" in capsys.readouterr().err


def test_selftest_prints_each_fault_and_the_accuracy(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    baseline = episode_dir(runs, "s1:p1") / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 5, "runs": 5, "cost_usd": 1}))
    monkeypatch.setattr(cli, "run_selftest", lambda *a, **k: [SelftestResult("weak-model", "model", "model"),
                                                              SelftestResult("truncated-prompt", "task", "harness")])
    assert cli.main(["selftest", "s1:p1", "--projects", str(tmp_path / "projects"), "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "weak-model: expected model, got model ✓" in out
    assert "truncated-prompt: expected task, got harness ✗" in out
    assert "1 of 2 planted faults named correctly" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_stage5.py -v`
Expected: FAIL — `invalid choice: 'review'` (SystemExit 2).

- [ ] **Step 3: Add the subcommands**

In `src/culprit/cli.py`, add to the imports:

```python
from culprit.calibration import (MIN_KAPPA, MIN_LABELS, UnknownRun, agreement, next_unlabelled, record_label,
                                 run_id, run_summary)
from culprit.selftest import CONTROL_MIN_PASSES, is_control, run_selftest, selftest_accuracy
```

Add `UnknownRun` to the `except` tuple in `main`.

Add before `return parser` in `build_parser`:

```python
    review = commands.add_parser("review", help="show the next judged replay you have not labelled")
    review.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    review.set_defaults(handler=review_command)

    label = commands.add_parser("label", help="record your own pass/fail for one judged replay")
    label.add_argument("run")
    label.add_argument("verdict", choices=["pass", "fail"])
    label.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    label.set_defaults(handler=label_command)

    calibration = commands.add_parser("calibration", help="how often the judge agrees with your labels")
    calibration.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    calibration.set_defaults(handler=calibration_command)

    selftest = commands.add_parser("selftest", help="plant known faults into a control episode and score culprit")
    selftest.add_argument("episode_id")
    _projects_and_runs(selftest)
    selftest.add_argument("--n", type=int, default=5)
    selftest.add_argument("--budget-per-fault", type=float, default=40.0)
    selftest.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    selftest.set_defaults(handler=selftest_command)
```

Add at the end of the file:

```python
def trust_lines(runs_dir: Path) -> list[str]:
    result = agreement(runs_dir)
    kappa = "not computable yet" if result.kappa is None else f"{result.kappa:.2f}"
    state = "calibrated" if result.calibrated else f"uncalibrated (needs {MIN_LABELS} labels and kappa ≥ {MIN_KAPPA})"
    correct, total = selftest_accuracy(runs_dir)
    planted = f"selftest: {correct} of {total} planted faults named correctly" if total else "selftest: not run"
    return [f"judge: {result.agreed} of {result.labelled} labels agree, kappa {kappa} — {state}", planted]


def review_command(args: argparse.Namespace) -> int:
    run_dir = next_unlabelled(args.runs)
    if run_dir is None:
        print("every judged replay is labelled")
        return 0
    print(f"{run_id(args.runs, run_dir)}\n{run_summary(run_dir)}")
    print(f"\nlabel it: culprit label {run_id(args.runs, run_dir)} pass|fail")
    return 0


def label_command(args: argparse.Namespace) -> int:
    record_label(args.runs, args.run, args.verdict == "pass")
    print(f"labelled {args.run} {args.verdict}")
    return 0


def calibration_command(args: argparse.Namespace) -> int:
    result = agreement(args.runs)
    kappa = "not computable yet" if result.kappa is None else f"{result.kappa:.2f}"
    print(f"{result.labelled} labelled, {result.agreed} agree with the judge, kappa {kappa}")
    print("calibrated" if result.calibrated else f"uncalibrated: needs {MIN_LABELS} labels and kappa ≥ {MIN_KAPPA}")
    return 0


def selftest_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    if not is_control(args.runs, args.episode_id):
        print(f"culprit: {args.episode_id} is not a control — its baseline must pass at least "
              f"{CONTROL_MIN_PASSES} of 5 (run `culprit baseline {args.episode_id}`)", file=sys.stderr)
        return 1
    results = run_selftest(episode, criterion, baseline_settings(episode, args.replay_budget), runs_dir=args.runs,
                           follow_ups=follow_up_prompts(args.projects, args.episode_id), n=args.n,
                           budget_per_fault=args.budget_per_fault)
    for result in results:
        print(f"{result.fault}: expected {result.expected}, got {result.got} {'✓' if result.correct else '✗'}")
    print(f"{sum(r.correct for r in results)} of {len(results)} planted faults named correctly")
    return 0
```

In `diagnose_command`, after the `for cause, reason in verdict.untested.items():` loop and before `return 0`, add:

```python
    for line in trust_lines(args.runs):
        print(line)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: 123 passed (Stage 4's `test_diagnose_passes_everything_through_and_prints_the_verdict` still passes: it checks substrings, and the trust lines only add output).

- [ ] **Step 5: Prove it on real runs**

Label the real replays Stages 3–4 produced, reading each summary yourself:

```bash
uv run culprit review          # read it, then:
uv run culprit label <run-id> pass   # or fail; repeat until "every judged replay is labelled"
uv run culprit calibration
```

Expected: the label count equals the number of judged replays so far; kappa prints as a number or "not computable yet". Any disagreement: open that run's `judgement.json` reason and decide whether the judge or your reading was wrong; if the judge, note the pattern for `judging_prompt`.

Then a selftest. From Stage 1's queue pick an episode the human marked `no-failure` that is replayable, or any replayable episode, and run its baseline; it qualifies as a control once the baseline passes at least 4 of 5:

```bash
uv run culprit criterion draft <CONTROL> && uv run culprit criterion approve <CONTROL>
uv run culprit restore <CONTROL> && uv run culprit baseline <CONTROL>
uv run culprit selftest <CONTROL> --n 3 --budget-per-fault 25
```

Expected: five lines `fault: expected X, got Y ✓|✗` and an accuracy line. A ✗ on `weak-model` with got `task` or `agent-setup` is a legitimate finding (a cheaper fix rescued the weak model), not a bug: record it. A ✗ on `impossible-criterion` is a bug: the check must fail every run. Record the per-fault lines in the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/culprit/cli.py tests/test_cli_stage5.py
git commit -m "Add culprit review, label, calibration and selftest; print trust figures with each verdict

Proven: <L> replays labelled, kappa <k>; selftest on <CONTROL>: <per-fault results>.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # must print nothing
```
