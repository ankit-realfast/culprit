"""Replaying an episode: its starting files restored into a throwaway copy, then one locked-down headless run."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from culprit import logs
from culprit.logs import (Episode, Tracked, is_follow_up, original_effort, prompt_images, recorded_answers,
                          records_before)
from culprit.stubs import MCP_PREFIX, PERMISSION_PROMPT_TOOL, SERVER as ANSWER_SERVER, recordings_from_episode


# --- sandbox: Rebuild the files an episode started from, in a throwaway folder, and check the rebuild. ---
# The folder is a clone of the working directory as it is today with every git remote removed. Each file
# with a snapshot backup is put back as the snapshot recorded it; then every file the episode itself
# recorded before touching it (an Edit's or Write's originalFile, or a Write that created the file) is
# set from that record, which is exact. A snapshot entry with no backup proves nothing about the file
# (measured 2026-10-11), so it is reported as unknown and left as it is today, like every untracked file.

DEFAULT_FILE_HISTORY = Path.home() / ".claude" / "file-history"


@dataclass(frozen=True)
class RestoreReport:
    workdir: Path
    restored: int
    unknown: tuple[str, ...]  # tracked with no backup: the starting content is not known
    missing_backups: tuple[str, ...]
    outside_cwd: tuple[str, ...]


@dataclass(frozen=True)
class GitRestore:
    commit: str
    restored: int
    removed: int


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
            file_history_root: Path = DEFAULT_FILE_HISTORY, session_root: Path | None = None) -> RestoreReport:
    cwd = Path(episode.cwd)
    clone_tree(cwd, destination)
    strip_git_remotes(destination)
    backups = file_history_root / episode.session_id
    restored = 0
    unknown: list[str] = []
    missing: list[str] = []
    outside: list[str] = []
    for tracked_path, backup_name in tracked.items():
        target = target_inside(cwd, destination, tracked_path, relative_base=session_root)
        if target is None:
            outside.append(tracked_path)
            continue
        if backup_name is None:
            unknown.append(tracked_path)
            continue
        backup = backups / backup_name
        if not backup.is_file():
            missing.append(tracked_path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup, target)
        restored += 1
    return RestoreReport(destination, restored, tuple(unknown), tuple(missing), tuple(outside))


def restore_from_git(episode: Episode, workdir: Path, covered: set[str]) -> GitRestore | None:
    """For a project in git, put every file nothing else recorded back as it was in the last commit before
    the episode began; a file added after that commit is removed. None outside git or with no such commit.

    Read-only on the original: --no-optional-locks keeps git from even refreshing its index."""
    cwd = Path(episode.cwd)
    git = ["git", "--no-optional-locks", "-C", str(cwd)]
    inside = subprocess.run([*git, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if inside.returncode != 0 or inside.stdout.strip() != "true" or not episode.started_at:
        return None
    commit = subprocess.run([*git, "rev-list", "-1", f"--before={episode.started_at}", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    if not commit:
        return None
    changed = subprocess.run([*git, "diff", "--name-only", "--relative", "-z", commit],
                             capture_output=True, text=True).stdout.split("\0")
    restored = removed = 0
    for relative in filter(None, changed):
        if str(cwd / relative) in covered:
            continue  # the snapshot or the episode's own record is closer in time than any commit
        target = target_inside(cwd, workdir, relative)
        if target is None:
            continue
        shown = subprocess.run([*git, "show", f"{commit}:./{relative}"], capture_output=True)
        if shown.returncode == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(shown.stdout)
            restored += 1
        elif target.is_file() or target.is_symlink():
            target.unlink()
            removed += 1
    return GitRestore(commit, restored, removed)


def episode_originals(episode: Episode) -> dict[str, str | None]:
    """Each file's state before the episode first touched it, where the episode recorded it:
    the content an Edit or Write saw, or None for a file a Write created."""
    originals: dict[str, str | None] = {}
    seen: set[str] = set()
    for record in episode.records:
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or not result.get("filePath"):
            continue
        path = result["filePath"]
        if path in seen:
            continue
        seen.add(path)
        if result.get("type") == "create":
            originals[path] = None
        elif isinstance(result.get("originalFile"), str):
            originals[path] = result["originalFile"]
    return originals


def apply_episode_originals(episode: Episode, workdir: Path) -> tuple[int, int]:
    """Set every file the episode recorded to that recorded starting state: (written, removed)."""
    cwd = Path(episode.cwd)
    written = removed = 0
    for path, original in episode_originals(episode).items():
        target = target_inside(cwd, workdir, path)
        if target is None:
            continue
        if original is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
                removed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original)
        written += 1
    return written, removed


def check_restore(episode: Episode, workdir: Path) -> tuple[int, list[RestoreMismatch]]:
    """Compare the restored copy with each file's state at the episode's first touch, where recorded.

    Run it before apply_episode_originals: it measures how far the snapshot alone can be trusted."""
    cwd = Path(episode.cwd)
    checked = 0
    mismatches: list[RestoreMismatch] = []
    for path, original in episode_originals(episode).items():
        target = target_inside(cwd, workdir, path)
        if target is None:
            continue
        checked += 1
        if original is None:
            if target.exists():
                mismatches.append(RestoreMismatch(path, "exists, but the episode created it"))
        elif not target.is_file():
            mismatches.append(RestoreMismatch(path, "missing after restore"))
        elif target.read_text(errors="replace") != original:
            mismatches.append(RestoreMismatch(path, "differs from the file the episode's first edit saw"))
    return checked, mismatches


def target_inside(cwd: Path, destination: Path, tracked_path: str,
                   relative_base: Path | None = None) -> Path | None:
    """Where a path from the episode lands in the restored copy, or None if it falls outside it.

    A relative path is read against relative_base (the session's starting directory, for snapshot
    paths) or against cwd when none is given."""
    path = Path(tracked_path)
    if not path.is_absolute():
        path = (relative_base or cwd) / path
    try:
        path = path.relative_to(cwd)
    except ValueError:
        return None
    target = (destination / path).resolve()
    if not target.is_relative_to(destination.resolve()):
        return None
    return target


# --- replay ---
# One replay: an episode re-run through a locked-down `claude -p` in a fresh clone of its starting files.
# The settings carry every knob an oracle may change — prompt, model, appended instructions, tools,
# context, setup script, spend cap — so a variant is the baseline with some fields replaced.
# A follow-up episode (not its session's first prompt) also starts from its session's earlier conversation:
# a copy of the log cut before the episode's prompt, under a fresh id and pointed at the clone, is written
# where Claude Code looks for the clone's sessions and resumed as a fork (verified live on 2.1.296).

NO_MCP_SERVERS = '{"mcpServers":{}}'
SAFETY_DENIED_TOOLS = ("Bash(git push *)", "Bash(gh *)")
REPLAY_PERMISSION_MODES = ("auto", "acceptEdits")
# Each replay's caps follow what the original episode took: three times its cost and its duration, never below the
# floors. The cost comes from the session's running total and is usually unknown (then the floor applies).
REPLAY_TIMEOUT_S = 1800  # the floor of the time limit
REPLAY_TIMEOUT_MULTIPLE = 3
REPLAY_BUDGET_FLOOR_USD = 10.0
REPLAY_BUDGET_MULTIPLE = 3
CHECK_TIMEOUT_S = 600
MAX_EFFORT = "max"  # the highest `claude --effort` level (2.1.296: low, medium, high, xhigh, max)
OUTPUT_TAIL_CHARS = 2000
REPLAY_DIR_PREFIX = ".culprit-replay-"
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
# Only infrastructure failures are void (not counted, retried): an API error, the CLI failing during execution,
# crashing or never starting. A run that reaches one of culprit's own limits ends early but is still a run, scored
# on what it produced, so running out of room shows in the pass rates (Inspect AI scores limited samples the same way).
VOID_SUBTYPES = ("error_during_execution",)
BUDGET_LIMIT_SUBTYPE = "error_max_budget_usd"
LIMIT_BUDGET = "budget"
LIMIT_TIME = "time"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
DIFF_EXCLUDES = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".next",
                 "target")
DIFF_TIMEOUT_S = 120
MAX_DIFF_BYTES = 2_000_000
# the OS-enforced Bash sandbox: shell commands may write only in the replay's copy and the temp folder,
# and may not fall back to running unsandboxed (verified live 2026-10-11: a write to ~ was refused)
SANDBOX_SETTINGS = {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}
GIT_STATE_FILES = ("HEAD", "index")
# what each replay leaves in its run folder
TRANSCRIPT_FILE = "transcript.jsonl"
DIFF_FILE = "diff.patch"
RESULT_FILE = "result.json"
MEMORY_DIR = "memory"  # also the name of a project's memory folder under Claude Code's projects


@dataclass(frozen=True)
class ReplaySettings:
    prompt: str
    model: str | None
    permission_mode: str
    effort: str | None = None  # --effort; None leaves Claude Code's default
    claude_md_addition: str | None = None  # standing instructions appended to the clone's project CLAUDE.md
    tools: str | None = None  # None keeps Claude Code's default tool set
    disallowed_tools: tuple[str, ...] = ()  # restrictions beyond SAFETY_DENIED_TOOLS
    context_preamble: str | None = None
    setup_script: Path | None = None
    max_budget_usd: float = REPLAY_BUDGET_FLOOR_USD
    timeout_s: float = REPLAY_TIMEOUT_S  # the time limit, from the episode's duration
    prompt_images: tuple[dict, ...] = ()  # image blocks the human attached, resent with the prompt
    mcp_recordings: dict = field(default_factory=dict)  # per MCP server, the calls the episode made and their results
    recorded_answers: dict = field(default_factory=dict)  # question -> what the human answered in the original
    history_file: Path | None = None  # a follow-up's session log: the replay resumes its records before...
    history_before: str | None = None  # ...the record with this uuid, the episode's prompt


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
    void_reason: str | None = None  # set when infrastructure failed the run: it does not count
    limit: str | None = None  # LIMIT_BUDGET or LIMIT_TIME when the run stopped at culprit's cap: it still counts
    usage: dict = field(default_factory=dict)  # token counts, by USAGE_KEYS
    model: str | None = None  # the model id the run actually ran as (an alias resolved), from its stream


class SetupScriptFailed(RuntimeError):
    pass


class OriginalModified(RuntimeError):
    """A replay changed the user's original project: stop everything and tell the human."""


def replay_permission_mode(original: str | None) -> str:
    # auto and acceptEdits replay as recorded. Every other mode becomes acceptEdits: in default and manual
    # the human approved prompts and nobody is present now; plan makes no edits; bypassPermissions is
    # never handed to a replay; the CLI no longer accepts default at all.
    return original if original in REPLAY_PERMISSION_MODES else "acceptEdits"


def replay_budget_usd(episode: Episode) -> float:
    """The per-replay spend cap: REPLAY_BUDGET_MULTIPLE × what the episode cost, at least the floor."""
    cost = logs.episode_cost_usd(episode)
    return max(REPLAY_BUDGET_FLOOR_USD, round(REPLAY_BUDGET_MULTIPLE * cost, 2) if cost else 0.0)


def replay_timeout_s(episode: Episode) -> float:
    """The per-replay time limit: REPLAY_TIMEOUT_MULTIPLE × how long the episode took, at least the floor."""
    return max(REPLAY_TIMEOUT_S, round(REPLAY_TIMEOUT_MULTIPLE * logs.signals_for(episode).duration_s))


def baseline_settings(episode: Episode, max_budget_usd: float | None = None) -> ReplaySettings:
    return ReplaySettings(prompt=episode.prompt_text, model=episode.model, effort=original_effort(episode),
                          permission_mode=replay_permission_mode(episode.permission_mode),
                          max_budget_usd=replay_budget_usd(episode) if max_budget_usd is None else max_budget_usd,
                          timeout_s=replay_timeout_s(episode), prompt_images=tuple(prompt_images(episode)),
                          mcp_recordings=recordings_from_episode(episode),
                          recorded_answers=recorded_answers(episode), **_history(episode))


def _history(episode: Episode) -> dict:
    if episode.prompt_uuid and is_follow_up(episode):
        return {"history_file": episode.session_file, "history_before": episode.prompt_uuid}
    return {}


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
                   recordings_file: Path | None = None, answers_file: Path | None = None,
                   resume_id: str | None = None) -> list[str]:
    command = [
        # CULPRIT_CLAUDE_BIN replays on another Claude Code build: a verdict that flips between builds points
        # at Claude Code itself rather than at your setup
        os.environ.get("CULPRIT_CLAUDE_BIN", "claude"), "-p",
        "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--mcp-config", _mcp_config(settings, recordings_file, answers_file),
        *_permission_host(settings, answers_file),
        "--permission-mode", settings.permission_mode,
        "--no-session-persistence",
        "--max-budget-usd", f"{settings.max_budget_usd:.2f}",
        "--settings", json.dumps(_applied_settings(memory_dir)),
    ]
    if resume_id:
        # the snapshot stays off so the fork reads the project's instructions as they are now, the agent-setup
        # oracle's addition included (a live probe found CLAUDE.md is re-read either way; this keeps it so)
        command += ["--resume", resume_id, "--fork-session", "--system-prompt-snapshot", "off"]
    if settings.prompt_images:
        command += ["--input-format", "stream-json"]  # the only way to send images headless (verified live)
    if settings.model:
        command += ["--model", settings.model]
    if settings.effort:
        command += ["--effort", settings.effort]
    if settings.tools is not None:
        command += ["--tools", settings.tools]
    denied = [*SAFETY_DENIED_TOOLS, *settings.disallowed_tools]
    if original_cwd:
        # "//" makes a permission path absolute: file tools may not touch the original project
        denied += [f"{tool}(/{original_cwd}/**)" for tool in WRITE_TOOLS]
    if settings.mcp_recordings:
        # the stand-in reaches nothing real, so its tools need no approval
        command += ["--allowedTools", *(f"{MCP_PREFIX}{server}" for server in settings.mcp_recordings)]
    command += ["--disallowedTools", *denied]
    return command


def _mcp_config(settings: ReplaySettings, recordings_file: Path | None, answers_file: Path | None) -> str:
    """No real MCP server ever: the stand-in in place of each server the episode used, plus culprit's
    answer host when the episode recorded answers to questions."""
    servers = {}
    if settings.mcp_recordings and recordings_file is not None:
        servers = {server: {"command": sys.executable,
                            "args": ["-m", "culprit.stubs", "mcp", str(recordings_file), server]}
                   for server in settings.mcp_recordings}
    if settings.recorded_answers and answers_file is not None:
        servers[ANSWER_SERVER] = {"command": sys.executable,
                                  "args": ["-m", "culprit.stubs", "answers", str(answers_file)]}
    return json.dumps({"mcpServers": servers}) if servers else NO_MCP_SERVERS


def _permission_host(settings: ReplaySettings, answers_file: Path | None) -> list[str]:
    """Nobody answers permission prompts, unless recorded answers exist: then culprit's answer host does,
    which is the only way Claude Code offers AskUserQuestion headless (verified live on 2.1.296)."""
    if settings.recorded_answers and answers_file is not None:
        return ["--permission-prompts", "host", "--permission-prompt-tool", PERMISSION_PROMPT_TOOL]
    return ["--permission-prompts", "none"]


def _applied_settings(memory_dir: Path | None) -> dict:
    applied: dict = {"sandbox": SANDBOX_SETTINGS}
    if memory_dir is not None:
        applied["autoMemoryDirectory"] = str(memory_dir)
    return applied


def _point_at_clone(text: str, original_cwd: Path, workdir: Path) -> str:
    return re.sub(re.escape(str(original_cwd)) + r"(?![\w.-])", lambda _: str(workdir), text)


def forked_history(records: list[dict], session_id: str, original_cwd: Path | None, workdir: Path) -> list[dict]:
    """The earlier records as the clone's own session: every sessionId the new id, and every path into the
    original project (each cwd, and any mentioned in the conversation) pointed at the clone."""

    def rewrite(value):
        if isinstance(value, dict):
            return {key: session_id if key == "sessionId" else rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str) and original_cwd is not None:
            return _point_at_clone(value, original_cwd, workdir)
        return value

    return [rewrite(record) for record in records]


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
    """subprocess.run, except a timeout or an interrupt kills everything the command started.

    The command runs in its own session, so the terminal's Ctrl-C never reaches it: culprit kills it."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE if capture_output else None,
                               stderr=subprocess.PIPE if capture_output else None, text=text, cwd=cwd, env=env,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired as timed_out:
        os.killpg(process.pid, signal.SIGKILL)
        # what the command printed before it was killed: a replay stopped at its time limit is still judged on it
        timed_out.stdout, timed_out.stderr = process.communicate()
        raise
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


# The agent-setup oracle's instructions reach Claude the way the real fix would: appended to the project's
# CLAUDE.md in the clone (a user message), not through --append-system-prompt (a stronger system-prompt
# channel that could pass with text that would fail once pasted into CLAUDE.md). The addition comes out
# again before the diff, so the judge sees only what the agent changed.
CLAUDE_MD_HEADING = "## More working rules"


def claude_md_target(root: Path) -> str:
    """The project instruction file the addition goes into: CLAUDE.md, or .claude/CLAUDE.md when only that
    exists; a new CLAUDE.md when neither does."""
    if not (root / "CLAUDE.md").is_file() and (root / ".claude" / "CLAUDE.md").is_file():
        return ".claude/CLAUDE.md"
    return "CLAUDE.md"


def _add_to_claude_md(workdir: Path, text: str) -> tuple[Path, str | None]:
    """Appends text under CLAUDE_MD_HEADING; returns the file and what it held before (None: it was new)."""
    target = workdir / claude_md_target(workdir)
    before = target.read_text() if target.is_file() else None
    lead = "" if not before else ("\n" if before.endswith("\n") else "\n\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{before or ''}{lead}{CLAUDE_MD_HEADING}\n\n{text.strip()}\n")
    return target, before


def _take_back_from_claude_md(target: Path, before: str | None, added: str) -> None:
    """Removes the addition again, keeping anything the agent itself wrote to the file."""
    if not target.is_file():
        return
    now = target.read_text()
    if now == added:
        if before is None:
            target.unlink()
        else:
            target.write_text(before)
        return
    block = added[len(before or ""):]
    if block in now:
        target.write_text(now.replace(block, "", 1))


def run_replay(settings: ReplaySettings, pristine: Path, run_dir: Path, workdir: Path, checks: tuple[str, ...],
               run=run_in_own_process_group, projects_dir: Path | None = None,
               original_cwd: Path | None = None, original_memory: Path | None = None) -> ReplayResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = None
    if original_memory and original_memory.is_dir():
        # a copy, so whatever the replay writes to its memory never reaches the real project's notes
        memory_dir = (run_dir / MEMORY_DIR).resolve()
        shutil.rmtree(memory_dir, ignore_errors=True)
        shutil.copytree(original_memory, memory_dir)
    answers_file = None
    if settings.recorded_answers:
        answers_file = (run_dir / "answers.json").resolve()
        answers_file.write_text(json.dumps(settings.recorded_answers))
    recordings_file = None
    if settings.mcp_recordings:
        recordings_file = (run_dir / "mcp-recordings.json").resolve()
        recordings_file.write_text(json.dumps(settings.mcp_recordings))
    # read before anything is made, so a session log that lost the prompt refuses the replay cleanly
    history = records_before(settings.history_file, settings.history_before) if settings.history_file else None
    clone_tree(pristine, workdir)
    # Claude Code makes a folder for the clone among its projects; it is removed afterwards if left empty
    project_folder = (projects_dir or logs.DEFAULT_PROJECTS) / project_folder_name(workdir.resolve())
    resume_id = forked_log = None
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
        addition = None
        if settings.claude_md_addition:
            text = settings.claude_md_addition
            if original_cwd:
                text = _point_at_clone(text, original_cwd, workdir)  # paths in the original point at the copy
            target, held = _add_to_claude_md(workdir, text)
            addition = (target, held, target.read_text())
        if history is not None:
            resume_id = str(uuid.uuid4())
            forked_log = project_folder / f"{resume_id}.jsonl"
            project_folder.mkdir(parents=True, exist_ok=True)
            forked = forked_history(history, resume_id, original_cwd, workdir.resolve())
            forked_log.write_text("".join(json.dumps(record) + "\n" for record in forked))
        try:
            completed = run(replay_command(settings, original_cwd, workdir, memory_dir, recordings_file, answers_file,
                                           resume_id),
                            cwd=workdir,
                            capture_output=True,
                            text=True, timeout=settings.timeout_s, env=env,
                            input=_stdin_for(settings, replay_prompt(settings, original_cwd, workdir)))
            stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as timed_out:
            stdout, exit_code = _text(timed_out.stdout), -1
            stderr = f"{_text(timed_out.stderr)}\ntimed out after {settings.timeout_s:.0f}s"
        (run_dir / TRANSCRIPT_FILE).write_text(stdout)
        (run_dir / "stderr.txt").write_text(stderr)
        result_line = _result_line(stdout)
        check_results = tuple(_run_check(command, workdir, run, env) for command in checks)
        if addition is not None:
            _take_back_from_claude_md(*addition)
        (run_dir / DIFF_FILE).write_text(_bounded_diff(pristine, workdir, run))
        if exit_code == -1:
            # stopped at the time limit: counted and judged on its diff, checks and tool calls; the cost is unknown,
            # so it is charged at the cap rather than as free
            replay = ReplayResult(run_dir, exit_code, True,
                                  f"stopped at the {settings.timeout_s:.0f}s time limit before a final answer",
                                  settings.max_budget_usd, 0, check_results, limit=LIMIT_TIME,
                                  usage=_streamed_usage(stdout), model=ran_as(stdout))
        elif result_line is None:
            reason = f"no result line (exit {exit_code})"
            # the CLI crashed or never started: infrastructure, so void; charged at the cap as its cost is unknown
            replay = ReplayResult(run_dir, exit_code, True, f"{reason}: {stderr[-300:]}", settings.max_budget_usd, 0,
                                  check_results, void_reason=reason, usage=_streamed_usage(stdout),
                                  model=ran_as(stdout))
        else:
            limit = LIMIT_BUDGET if result_line.get("subtype") == BUDGET_LIMIT_SUBTYPE else None
            replay = ReplayResult(run_dir, exit_code, bool(result_line.get("is_error")),
                                  str(result_line.get("result") or ""), float(result_line.get("total_cost_usd") or 0),
                                  int(result_line.get("num_turns") or 0), check_results,
                                  void_reason=_void_reason(result_line), limit=limit,
                                  usage=_counts(result_line.get("usage")) or _streamed_usage(stdout),
                                  model=ran_as(stdout))
        (run_dir / RESULT_FILE).write_text(json.dumps(asdict(replay), default=str, indent=2))
        if watched:
            after = tree_fingerprint(watched)
            changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
            if changed:
                raise OriginalModified(f"files in {watched} changed during a replay: {', '.join(changed[:5])}"
                                       f"{' …' if len(changed) > 5 else ''} — check them before running anything else")
        return replay
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if forked_log is not None:
            forked_log.unlink(missing_ok=True)
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


def _counts(usage: object) -> dict:
    """The token counts in a usage block, by USAGE_KEYS."""
    if not isinstance(usage, dict):
        return {}
    return {key: int(usage[key]) for key in USAGE_KEYS if isinstance(usage.get(key), int)}


def _streamed_usage(stdout: str) -> dict:
    """With no result line, the tokens the assistant messages reported, each message once (stream-json repeats a
    message's usage on every event it sends for it)."""
    by_message: dict[str, dict] = {}
    for number, line in enumerate(stdout.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record, dict) and record.get("type") == "assistant" else None
        if isinstance(message, dict) and (counts := _counts(message.get("usage"))):
            by_message[str(message.get("id") or number)] = counts
    total: dict[str, int] = {}
    for counts in by_message.values():
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
    return total


def ran_as(stdout: str) -> str | None:
    """The model id a stream-json run ran as: the init event names it (an alias already resolved); without one, the
    first assistant message that names a real model (Claude Code writes `<synthetic>` for messages it made up)."""
    fallback = None
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "system" and record.get("subtype") == "init" and record.get("model"):
            return str(record["model"])
        message = record.get("message") if record.get("type") == "assistant" else None
        model = message.get("model") if isinstance(message, dict) else None
        if fallback is None and isinstance(model, str) and model and not model.startswith("<"):
            fallback = model
    return fallback


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output or ""


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
    if all(entry.name == MEMORY_DIR and entry.is_dir() and not any(entry.iterdir()) for entry in entries):
        for entry in entries:
            entry.rmdir()
        project_folder.rmdir()


def settings_to_json(settings: ReplaySettings) -> dict:
    data = asdict(settings)
    data["setup_script"] = str(settings.setup_script) if settings.setup_script else None
    data["disallowed_tools"] = list(settings.disallowed_tools)
    data["prompt_images"] = list(settings.prompt_images)
    data["history_file"] = str(settings.history_file) if settings.history_file else None
    return data


def settings_from_json(data: dict) -> ReplaySettings:
    fields = dict(data)
    if "append_system_prompt" in fields:  # stored before the addition moved into CLAUDE.md
        fields.setdefault("claude_md_addition", fields.pop("append_system_prompt"))
    fields["setup_script"] = Path(fields["setup_script"]) if fields.get("setup_script") else None
    fields["disallowed_tools"] = tuple(fields.get("disallowed_tools") or ())
    fields["prompt_images"] = tuple(fields.get("prompt_images") or ())
    fields["mcp_recordings"] = dict(fields.get("mcp_recordings") or {})
    fields["recorded_answers"] = dict(fields.get("recorded_answers") or {})
    fields["history_file"] = Path(fields["history_file"]) if fields.get("history_file") else None
    return ReplaySettings(**fields)
