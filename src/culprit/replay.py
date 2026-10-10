"""One replay: an episode re-run through a locked-down `claude -p` in a fresh clone of its starting files.

The settings carry every knob an oracle may change — prompt, model, appended instructions, tools,
context, setup script, spend cap — so a variant is the baseline with some fields replaced.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from culprit.claude import NO_MCP_SERVERS
from culprit.episodes import Episode, content_blocks
from culprit.mcp_stub import MCP_PREFIX, recordings_from_episode
from culprit.sandbox import clone_tree

SAFETY_DENIED_TOOLS = ("Bash(git push *)", "Bash(gh *)")
REPLAY_PERMISSION_MODES = ("auto", "acceptEdits")
REPLAY_TIMEOUT_S = 1800
CHECK_TIMEOUT_S = 600
DEFAULT_REPLAY_BUDGET_USD = 2.0
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
OUTPUT_TAIL_CHARS = 2000
REPLAY_DIR_PREFIX = ".culprit-replay-"
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
VOID_SUBTYPES = ("error_max_budget_usd", "error_during_execution")
DIFF_EXCLUDES = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".next",
                 "target")
DIFF_TIMEOUT_S = 120
MAX_DIFF_BYTES = 2_000_000
# the OS-enforced Bash sandbox: shell commands may write only in the replay's copy and the temp folder,
# and may not fall back to running unsandboxed (verified live 2026-10-11: a write to ~ was refused)
SANDBOX_SETTINGS = {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}
GIT_STATE_FILES = ("HEAD", "index")


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
    prompt_images: tuple[dict, ...] = ()  # image blocks the human attached, resent with the prompt
    mcp_recordings: dict = field(default_factory=dict)  # per MCP server, the calls the episode made and their results


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
    void_reason: str | None = None  # set when the run failed for a reason outside the agent: it does not count


class SetupScriptFailed(RuntimeError):
    pass


class OriginalModified(RuntimeError):
    """A replay changed the user's original project: stop everything and tell the human."""


def replay_permission_mode(original: str | None) -> str:
    # auto and acceptEdits replay as recorded. Every other mode becomes acceptEdits: in default and manual
    # the human approved prompts and nobody is present now; plan makes no edits; bypassPermissions is
    # never handed to a replay; the CLI no longer accepts default at all.
    return original if original in REPLAY_PERMISSION_MODES else "acceptEdits"


def baseline_settings(episode: Episode, max_budget_usd: float = DEFAULT_REPLAY_BUDGET_USD) -> ReplaySettings:
    prompt_blocks = content_blocks(episode.records[0]) if episode.records else []
    images = tuple(block for block in prompt_blocks if block.get("type") == "image")
    return ReplaySettings(prompt=episode.prompt_text, model=episode.model,
                          permission_mode=replay_permission_mode(episode.permission_mode),
                          max_budget_usd=max_budget_usd, prompt_images=images,
                          mcp_recordings=recordings_from_episode(episode))


def full_prompt(settings: ReplaySettings) -> str:
    if settings.context_preamble:
        return f"{settings.context_preamble}\n\n{settings.prompt}"
    return settings.prompt


def replay_prompt(settings: ReplaySettings, original_cwd: Path | None = None, workdir: Path | None = None) -> str:
    """What the replay is asked, sent on stdin so a prompt starting with "-" is never read as an option."""
    prompt = full_prompt(settings)
    if original_cwd and workdir:
        # the human's text often names files by absolute path in the original project: point it at the copy
        prompt = _point_at_clone(prompt, original_cwd, workdir)
    return prompt


def replay_command(settings: ReplaySettings, original_cwd: Path | None = None,
                   workdir: Path | None = None, memory_dir: Path | None = None,
                   recordings_file: Path | None = None) -> list[str]:
    appended = settings.append_system_prompt
    if appended and original_cwd and workdir:
        appended = _point_at_clone(appended, original_cwd, workdir)
    command = [
        "claude", "-p",
        "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--mcp-config", _mcp_config(settings, recordings_file),
        "--permission-prompts", "none",
        "--permission-mode", settings.permission_mode,
        "--no-session-persistence",
        "--max-budget-usd", f"{settings.max_budget_usd:.2f}",
        "--settings", json.dumps(_applied_settings(memory_dir)),
    ]
    if settings.prompt_images:
        command += ["--input-format", "stream-json"]  # the only way to send images headless (verified live)
    if settings.model:
        command += ["--model", settings.model]
    if settings.tools is not None:
        command += ["--tools", settings.tools]
    if appended:
        command += ["--append-system-prompt", appended]
    denied = [*SAFETY_DENIED_TOOLS, *settings.disallowed_tools]
    if original_cwd:
        # "//" makes a permission path absolute: file tools may not touch the original project
        denied += [f"{tool}(/{original_cwd}/**)" for tool in WRITE_TOOLS]
    if settings.mcp_recordings:
        # the stand-in reaches nothing real, so its tools need no approval
        command += ["--allowedTools", *(f"{MCP_PREFIX}{server}" for server in settings.mcp_recordings)]
    command += ["--disallowedTools", *denied]
    return command


def _mcp_config(settings: ReplaySettings, recordings_file: Path | None) -> str:
    """No MCP server at all, or for an episode that used some, the stand-in in place of each one."""
    if not settings.mcp_recordings or recordings_file is None:
        return NO_MCP_SERVERS
    servers = {server: {"command": sys.executable, "args": ["-m", "culprit.mcp_stub", str(recordings_file), server]}
               for server in settings.mcp_recordings}
    return json.dumps({"mcpServers": servers})


def _applied_settings(memory_dir: Path | None) -> dict:
    applied: dict = {"sandbox": SANDBOX_SETTINGS}
    if memory_dir is not None:
        applied["autoMemoryDirectory"] = str(memory_dir)
    return applied


def _point_at_clone(text: str, original_cwd: Path, workdir: Path) -> str:
    return re.sub(re.escape(str(original_cwd)) + r"(?![\w.-])", lambda _: str(workdir), text)


def tree_fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """Size and modification time of every file in a project (git's HEAD and index included)."""
    fingerprint: dict[str, tuple[int, int]] = {}
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [d for d in subdirectories if d != ".git"]
        for name in files:
            path = Path(directory) / name
            stat = path.lstat()
            fingerprint[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    for name in GIT_STATE_FILES:
        path = root / ".git" / name
        if path.is_file():
            stat = path.stat()
            fingerprint[f".git/{name}"] = (stat.st_size, stat.st_mtime_ns)
    return fingerprint


def replay_workdir(episode: Episode) -> Path:
    """Beside the original working directory, so the replay loads the same ancestor CLAUDE.md files."""
    return Path(episode.cwd).parent / f"{REPLAY_DIR_PREFIX}{uuid.uuid4().hex[:12]}"


def project_folder_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def run_in_own_process_group(command: list[str], *, input: str | None = None, capture_output: bool = True,
                             text: bool = True, timeout: float | None = None, cwd: Path | None = None,
                             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """subprocess.run, except a timeout kills everything the command started, not just the command."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE if capture_output else None,
                               stderr=subprocess.PIPE if capture_output else None, text=text, cwd=cwd, env=env,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_replay(settings: ReplaySettings, pristine: Path, run_dir: Path, workdir: Path, checks: tuple[str, ...],
               run=run_in_own_process_group, projects_dir: Path = DEFAULT_PROJECTS,
               original_cwd: Path | None = None, original_memory: Path | None = None) -> ReplayResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = None
    if original_memory and original_memory.is_dir():
        # a copy, so whatever the replay writes to its memory never reaches the real project's notes
        memory_dir = (run_dir / "memory").resolve()
        shutil.rmtree(memory_dir, ignore_errors=True)
        shutil.copytree(original_memory, memory_dir)
    recordings_file = None
    if settings.mcp_recordings:
        recordings_file = (run_dir / "mcp-recordings.json").resolve()
        recordings_file.write_text(json.dumps(settings.mcp_recordings))
    clone_tree(pristine, workdir)
    project_folder = projects_dir / project_folder_name(workdir.resolve())
    # git must not climb out of the clone: beside a project inside a real repository it would find that
    # repository and a replay's `git commit` would land in it
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(workdir.parent.resolve())}
    watched = original_cwd if original_cwd and original_cwd.is_dir() else None
    before = tree_fingerprint(watched) if watched else None
    try:
        if settings.setup_script:
            # resolved first: the script path is usually relative to culprit's folder, and the clone is elsewhere
            setup = run(["bash", str(settings.setup_script.resolve())], cwd=workdir, capture_output=True, text=True,
                        timeout=CHECK_TIMEOUT_S, env=env)
            if setup.returncode != 0:
                raise SetupScriptFailed(f"{settings.setup_script} exited {setup.returncode}: {setup.stderr[-500:]}")
        try:
            completed = run(replay_command(settings, original_cwd, workdir, memory_dir, recordings_file), cwd=workdir,
                            capture_output=True,
                            text=True, timeout=REPLAY_TIMEOUT_S, env=env,
                            input=_stdin_for(settings, replay_prompt(settings, original_cwd, workdir)))
            stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, exit_code = "", f"timed out after {REPLAY_TIMEOUT_S}s", -1
        (run_dir / "transcript.jsonl").write_text(stdout)
        (run_dir / "stderr.txt").write_text(stderr)
        result_line = _result_line(stdout)
        check_results = tuple(_run_check(command, workdir, run, env) for command in checks)
        (run_dir / "diff.patch").write_text(_bounded_diff(pristine, workdir, run))
        if result_line is None:
            reason = "timed out" if exit_code == -1 else f"no result line (exit {exit_code})"
            # the cost is unknown, so it is charged at the cap rather than as free
            replay = ReplayResult(run_dir, exit_code, True, f"{reason}: {stderr[-300:]}", settings.max_budget_usd, 0,
                                  check_results, void_reason=reason)
        else:
            replay = ReplayResult(run_dir, exit_code, bool(result_line.get("is_error")),
                                  str(result_line.get("result") or ""), float(result_line.get("total_cost_usd") or 0),
                                  int(result_line.get("num_turns") or 0), check_results,
                                  void_reason=_void_reason(result_line))
        (run_dir / "result.json").write_text(json.dumps(asdict(replay), default=str, indent=2))
        if watched:
            after = tree_fingerprint(watched)
            changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
            if changed:
                raise OriginalModified(f"files in {watched} changed during a replay: {', '.join(changed[:5])}"
                                       f"{' …' if len(changed) > 5 else ''} — check them before running anything else")
        return replay
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_if_only_empty_memory(project_folder)


def _stdin_for(settings: ReplaySettings, prompt: str) -> str:
    if not settings.prompt_images:
        return prompt
    content = [*settings.prompt_images, {"type": "text", "text": prompt}]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def _bounded_diff(pristine: Path, workdir: Path, run) -> str:
    excludes = [f"--exclude={name}" for name in DIFF_EXCLUDES]
    try:
        diff = run(["diff", "-ruN", *excludes, str(pristine), str(workdir)], capture_output=True, text=True,
                   timeout=DIFF_TIMEOUT_S).stdout
    except subprocess.TimeoutExpired:
        return f"[diff timed out after {DIFF_TIMEOUT_S}s]\n"
    data = diff.encode()
    if len(data) <= MAX_DIFF_BYTES:
        return diff
    return data[:MAX_DIFF_BYTES].decode(errors="ignore") + f"\n[diff cut: {len(data) - MAX_DIFF_BYTES} more bytes]\n"


def _void_reason(result_line: dict) -> str | None:
    if result_line.get("api_error_status"):
        return f"API error {result_line['api_error_status']}"
    if result_line.get("subtype") in VOID_SUBTYPES:
        return str(result_line["subtype"])
    return None


def _result_line(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            return record
    return None


def _run_check(command: str, workdir: Path, run, env: dict[str, str]) -> CheckResult:
    try:
        done = run(["bash", "-c", command], cwd=workdir, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
                   env=env)
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


def settings_to_json(settings: ReplaySettings) -> dict:
    data = asdict(settings)
    data["setup_script"] = str(settings.setup_script) if settings.setup_script else None
    data["disallowed_tools"] = list(settings.disallowed_tools)
    data["prompt_images"] = list(settings.prompt_images)
    return data


def settings_from_json(data: dict) -> ReplaySettings:
    fields = dict(data)
    fields["setup_script"] = Path(fields["setup_script"]) if fields.get("setup_script") else None
    fields["disallowed_tools"] = tuple(fields.get("disallowed_tools") or ())
    fields["prompt_images"] = tuple(fields.get("prompt_images") or ())
    fields["mcp_recordings"] = dict(fields.get("mcp_recordings") or {})
    return ReplaySettings(**fields)
