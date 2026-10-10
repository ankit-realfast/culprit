"""A stand-in MCP server that only replays results recorded in the original session.

A replay connects to this instead of the real server, so a tool that once sent a Slack message or an
email answers with what it answered then and reaches nothing. A call the original session never made
gets an error saying so, which the replay sees like any failing tool.

Run as: python -m culprit.mcp_stub <recordings.json> <server name>
"""

from __future__ import annotations

import json
import sys

from culprit.episodes import Episode, content_blocks

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
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": "1"}}
    elif method == "tools/list":
        names = list(dict.fromkeys(recording["tool"] for recording in recordings))
        result = {"tools": [{"name": name, "description": "Replays the result recorded in the original session.",
                             "inputSchema": {"type": "object", "additionalProperties": True}} for name in names]}
    elif method == "tools/call":
        params = message.get("params") or {}
        match = next((r for r in recordings
                      if r["tool"] == params.get("name") and r["arguments"] == (params.get("arguments") or {})), None)
        if match is None:
            result = {"content": [{"type": "text", "text": NOT_RECORDED}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": match["text"]}], "isError": match["is_error"]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def main() -> None:
    recordings = json.loads(open(sys.argv[1]).read()).get(sys.argv[2], [])
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), recordings)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
