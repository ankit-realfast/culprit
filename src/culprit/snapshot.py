"""The file-history snapshot Claude Code took when the episode's prompt arrived.

It maps every file Claude Code had edited so far in the session to the backup holding that file's
content at this point, or to None for a file that did not exist yet (an assumption Stage 2's
restore check verifies on real episodes).
"""

from __future__ import annotations

from pathlib import Path

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


def session_root(session_file: Path) -> Path | None:
    """The directory the session started in: file-history stores its relative paths against it,
    not against the working directory of a later episode that changed directory."""
    for record in read_records(session_file):
        if record.get("cwd"):
            return Path(record["cwd"])
    return None
