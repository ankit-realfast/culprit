"""The one place culprit asks a model a question: a locked-down headless Claude call with a JSON schema.

No tools, no MCP servers, no permission prompts, no session log, and a spend cap on every call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from culprit.replay import NO_MCP_SERVERS

DRAFTING_MODEL = "claude-sonnet-5-5"
CALL_TIMEOUT_S = 600
CALL_CAP_USD = 0.50  # the most one call may spend, and what culprit charges for it (the CLI reports no cost)
# Calls run from their own empty folder, so they never read a project's CLAUDE.md and the empty
# memory folder Claude Code creates for them is easy to recognise under ~/.claude/projects.
CALLS_DIR = Path.home() / ".culprit" / "calls"

Ask = Callable[[str, dict], dict]


def as_messages(texts: list[str]) -> str:
    """The person's later messages, as the drafting and leak-check prompts quote them."""
    return "\n".join(f"<message>{text}</message>" for text in texts) or "<message>(none)</message>"


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


def ask_claude(prompt: str, schema: dict, model: str = DRAFTING_MODEL, max_budget_usd: float = CALL_CAP_USD,
               run=subprocess.run, cwd: Path = CALLS_DIR) -> dict:
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        completed = run(structured_call_command(prompt, schema, model, max_budget_usd),
                        capture_output=True, text=True, timeout=CALL_TIMEOUT_S, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ClaudeCallFailed(f"claude timed out after {CALL_TIMEOUT_S}s") from None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ClaudeCallFailed(f"claude exited {completed.returncode}: {completed.stderr.strip()[:500]}") from None
    if payload.get("is_error") or "structured_output" not in payload:
        reason = payload.get("result") or payload.get("subtype") or "no structured output"
        raise ClaudeCallFailed(f"claude returned no structured output: {reason}")
    return payload["structured_output"]
