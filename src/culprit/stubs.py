"""Stand-in MCP servers a replay connects to instead of real ones, each run as its own light process.

Run as: python -m culprit.stubs mcp <recordings.json> <server name>
    or: python -m culprit.stubs answers <answers.json>

This module imports nothing heavier than logs, so a stand-in process starts fast."""

from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher

from culprit.logs import ASK_USER_TOOL, Episode, content_blocks


# --- mcp_stub: A stand-in MCP server that only replays results recorded in the original session. ---
# A replay connects to this instead of the real server, so a tool that once sent a Slack message or an
# email answers with what it answered then and reaches nothing. A call the original session never made
# gets an error saying so, which the replay sees like any failing tool.

MCP_PREFIX = "mcp__"
SERVER_NAME = "culprit-replay-stub"
NOT_RECORDED = ("culprit replay: no recorded result for this call — the original session never made it, "
                "so the real service is not contacted")


def recordings_from_episode(episode: Episode) -> dict[str, list[dict]]:
    """Every MCP call the episode made, per server, paired with the result it got."""
    calls: dict[str, tuple[str, str, dict]] = {}
    recordings: dict[str, list[dict]] = {}
    for record in episode.records:
        for block in content_blocks(record):
            if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(MCP_PREFIX):
                parts = block["name"].split("__", 2)
                if len(parts) == 3:
                    calls[block.get("id")] = (parts[1], parts[2], block.get("input") or {})
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                server, tool, arguments = calls.pop(block["tool_use_id"])
                recordings.setdefault(server, []).append(
                    {"tool": tool, "arguments": arguments, "text": _result_text(block.get("content")),
                     "is_error": bool(block.get("is_error"))})
    return recordings


def handle(message: dict, recordings: list[dict]) -> dict | None:
    """Answer one JSON-RPC message; None for a notification, which gets no reply."""
    names = list(dict.fromkeys(recording["tool"] for recording in recordings))
    tools = [tool_spec(name, "Replays the result recorded in the original session.") for name in names]

    def call(name: str, arguments: dict) -> dict:
        match = next((r for r in recordings if r["tool"] == name and r["arguments"] == arguments), None)
        if match is None:
            return text_result(NOT_RECORDED, is_error=True)
        return text_result(match["text"], is_error=match["is_error"])

    return serve_message(message, tools, call)


def tool_spec(name: str, description: str) -> dict:
    """A tools/list entry that takes any arguments."""
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": True}}


def text_result(text: str, is_error: bool = False) -> dict:
    """A tools/call result holding one text block."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve_message(message: dict, tools: list[dict], call) -> dict | None:
    """The MCP JSON-RPC envelope shared by culprit's stand-in servers: initialize, tools/list, tools/call."""
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call(params.get("name"), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve_stdio(answer) -> None:
    """Read newline-delimited JSON-RPC from stdin and write each reply to stdout."""
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = answer(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def mcp_main(args: list[str]) -> None:
    """`mcp <recordings.json> <server name>`: serve one server's recorded results."""
    recordings = json.loads(open(args[0]).read()).get(args[1], [])
    serve_stdio(lambda message: handle(message, recordings))


# --- answer_host ---
# The replay's permission host: answers AskUserQuestion with what you answered in the original session.
# Claude Code offers AskUserQuestion headless only when a permission-prompt tool is configured
# (verified live on 2.1.296: a PreToolUse hook is not enough). This MCP tool is that host. A question
# close to one you answered gets your recorded answer; anything else is refused, exactly as a replay
# with no human present refuses every permission prompt.

SERVER = "culprit_answers"
TOOL = "answer"
PERMISSION_PROMPT_TOOL = f"mcp__{SERVER}__{TOOL}"
SAME_QUESTION_RATIO = 0.6  # how close a replay's wording must be to a question you answered
NO_HUMAN = "no human is present during a replay, so this permission prompt is refused"


def decide(request: dict, answers: dict[str, str]) -> dict:
    """The permission decision Claude Code expects back: allow with your answers, or deny with a reason."""
    if request.get("tool_name") != ASK_USER_TOOL:
        return {"behavior": "deny", "message": NO_HUMAN}
    questions = (request.get("input") or {}).get("questions") or []
    chosen = {}
    for question in questions:
        text = question.get("question", "")
        best = max(answers, key=lambda known: _similarity(known, text), default=None)
        if best is None or _similarity(best, text) < SAME_QUESTION_RATIO:
            return {"behavior": "deny", "message": f"you were not asked anything like {text!r} in the original session"}
        chosen[text] = answers[best]
    return {"behavior": "allow", "updatedInput": {"questions": questions, "answers": chosen}}


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def answers_main(args: list[str]) -> None:
    """`answers <answers.json>`: serve the permission host."""
    answers = json.loads(open(args[0]).read())
    tools = [tool_spec(TOOL, "Answers permission prompts during a culprit replay.")]

    def call(name: str, arguments: dict) -> dict:
        return text_result(json.dumps(decide(arguments, answers)))

    serve_stdio(lambda message: serve_message(message, tools, call))


# --- entry point ---

STAND_INS = {"mcp": mcp_main, "answers": answers_main}


def main() -> None:
    STAND_INS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
