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
REPLAY_PERMISSION_MODES = ("auto", "acceptEdits")
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
    # auto and acceptEdits replay as recorded. Every other mode becomes acceptEdits: in default and manual
    # the human approved prompts and nobody is present now; plan makes no edits; bypassPermissions is
    # never handed to a replay; the CLI no longer accepts default at all.
    return original if original in REPLAY_PERMISSION_MODES else "acceptEdits"


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
            # resolved first: the script path is usually relative to culprit's folder, and the clone is elsewhere
            setup = run(["bash", str(settings.setup_script.resolve())], cwd=workdir, capture_output=True, text=True,
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
