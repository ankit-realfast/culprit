"""Rebuild the files an episode started from, in a throwaway folder, and check the rebuild.

The folder is a clone of the working directory as it is today with every git remote removed. Each file
with a snapshot backup is put back as the snapshot recorded it; then every file the episode itself
recorded before touching it (an Edit's or Write's originalFile, or a Write that created the file) is
set from that record, which is exact. A snapshot entry with no backup proves nothing about the file
(measured 2026-10-11), so it is reported as unknown and left as it is today, like every untracked file.
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
