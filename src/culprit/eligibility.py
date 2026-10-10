"""Why an episode cannot be replayed in v1 (an empty list means it can), and which project hooks a replay runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from culprit.answer_host import ASK_USER_TOOL, recorded_answers
from culprit.episodes import Episode, prompt_images, tool_uses
from culprit.snapshot import Tracked

PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")


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
