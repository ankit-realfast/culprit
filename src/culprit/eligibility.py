"""Why an episode cannot be replayed in v1 (an empty list means it can), and which project hooks a replay runs."""

from __future__ import annotations

import json
from pathlib import Path

from culprit.episodes import Episode, tool_uses
from culprit.snapshot import Tracked

ASK_USER_TOOL = "AskUserQuestion"
MCP_TOOL_PREFIX = "mcp__"
PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")


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
