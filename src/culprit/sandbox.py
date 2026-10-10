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
