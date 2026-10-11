"""Stand-in MCP servers a replay connects to instead of real ones, each run as its own light process.

Run as: python -m culprit.stubs mcp <recordings.json> <server name>
    or: python -m culprit.stubs answers <answers.json>
    or: python -m culprit.stubs bash-hook <network-recordings.json>   (a PreToolUse hook)
    or: python -m culprit.stubs web-hook <network-recordings.json>    (a PreToolUse hook)

This module imports nothing heavier than logs, so a stand-in process starts fast."""

from __future__ import annotations

import json
import re
import shlex
import sys
from difflib import SequenceMatcher
from pathlib import Path

from culprit.logs import ASK_USER_TOOL, Episode, content_blocks


# --- mcp: A stand-in MCP server that only replays results recorded in the original session. ---
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


# --- answers ---
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


# --- bash-hook: recorded answers for shell commands that reach the network ---
# The replay's shell sandbox reaches no host (a strict, empty allow list). A shell command the episode ran
# against the network (curl, kubectl, psql, …) is answered instead from the log: culprit's PreToolUse hook
# rewrites a command that exactly matches a recorded one (after the original project's paths point at the
# clone, whitespace normalised) into one that prints the recorded output and exits with the recorded
# status. Anything else is left alone, so the sandbox refuses a live network call honestly. Every network
# call the hook sees is logged beside the recordings, served or not, so a verdict built on refused calls
# shows. `gh` and `git push` are not on the list: the replay keeps denying them outright.
# updatedInput rewriting a Bash command from a --settings hook was verified live on 2.1.296 (Haiku): the
# replacement's output and exit status came back and the transcript kept the original command.

NETWORK_TOOLS = ("curl", "wget", "gcloud", "gsutil", "bq", "psql", "mysql", "aws", "az", "kubectl", "ssh", "scp",
                 "sftp", "rsync", "http", "https", "redis-cli", "mongosh", "mongo", "sqlcmd", "firebase", "vercel",
                 "supabase", "heroku", "stripe", "nc", "telnet", "ftp", "doctl", "flyctl", "netlify", "wrangler",
                 "helm", "terraform")
COMMAND_WRAPPERS = ("sudo", "env", "time", "nohup", "command", "exec", "xargs", "nice")
SHELLS = ("bash", "sh", "zsh")
SEGMENT_BREAK = re.compile(r"&&|\|\||[;&|\n`(){}]|\$\(")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REMOTE_PATH = re.compile(r"^[^/\s:]+:")  # host:path or user@host:path, as rsync takes a remote over ssh
EXIT_CODE_LINE = re.compile(r"^Exit code (\d+)\n?")
NETWORK_RECORDINGS = "network-recordings.json"
NETWORK_LOG = "network-log.jsonl"
BASH_TOOL = "Bash"


def uses_network(command: str) -> bool:
    """Whether any simple command in the line runs a network tool (behind wrappers, assignments, paths)."""
    unquoted = command.replace("'", " ").replace('"', " ")
    return any(_runs_network_tool(segment.split()) for segment in SEGMENT_BREAK.split(unquoted))


def _runs_network_tool(words: list[str]) -> bool:
    while words:
        word = words[0]
        if ASSIGNMENT.match(word) or word in COMMAND_WRAPPERS or (word.startswith("-") and len(words) > 1):
            words = words[1:]
        elif word == "timeout" and len(words) > 1:
            words = words[2:]
        elif word in SHELLS and words[1:2] == ["-c"]:
            words = words[2:]
        else:
            break
    if not words:
        return False
    tool = words[0].rsplit("/", 1)[-1]
    if tool == "rsync":
        return any(arg in ("-e", "--rsh") or arg.startswith("--rsh=") or REMOTE_PATH.match(arg)
                   for arg in words[1:])
    return tool in NETWORK_TOOLS


def network_recordings_from_episode(episode: Episode) -> dict[str, list[dict]]:
    """Every network shell command the episode ran, in order, with the output and exit status it got
    ("shell"), and every WebFetch and WebSearch call with its result ("web")."""
    calls: dict[str, str] = {}
    web_calls: dict[str, tuple[str, dict]] = {}
    shell: list[dict] = []
    web: list[dict] = []
    for record in episode.records:
        for block in content_blocks(record):
            if block.get("type") == "tool_use" and block.get("name") == BASH_TOOL:
                command = str((block.get("input") or {}).get("command", ""))
                if uses_network(command):
                    calls[block.get("id")] = command
            elif block.get("type") == "tool_use" and block.get("name") in WEB_TOOLS:
                web_calls[block.get("id")] = (block["name"], _web_key(block["name"], block.get("input") or {}))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in web_calls:
                tool, key = web_calls.pop(block["tool_use_id"])
                web.append({"tool": tool, "input": key, "text": _result_text(block.get("content"))})
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                text = _result_text(block.get("content"))
                exit_code = 0
                if block.get("is_error"):
                    # Claude Code puts "Exit code N" before a failed command's output; it adds it again on replay
                    stated = EXIT_CODE_LINE.match(text)
                    exit_code = int(stated.group(1)) if stated else 1
                    text = text[stated.end():] if stated else text
                shell.append({"command": calls.pop(block["tool_use_id"]), "output": text, "exit_code": exit_code})
    return {name: found for name, found in (("shell", shell), ("web", web)) if found}


def normalise(command: str) -> str:
    return " ".join(command.split())


def prepare_network_recordings(recordings: dict, folder: Path, rewrite) -> Path:
    """Write the hook's recordings into the run folder: each command pointed at the clone by `rewrite` and
    normalised, each output in its own file the replacement command prints. A previous log is cleared."""
    folder.mkdir(parents=True, exist_ok=True)
    outputs = folder / "network-outputs"
    outputs.mkdir(exist_ok=True)
    shell = []
    for index, recording in enumerate(recordings.get("shell") or []):
        output_file = (outputs / f"{index}.txt").resolve()
        output_file.write_text(recording["output"])
        shell.append({"command": normalise(rewrite(recording["command"])), "output_file": str(output_file),
                      "exit_code": int(recording["exit_code"])})
    path = (folder / NETWORK_RECORDINGS).resolve()
    path.write_text(json.dumps({"shell": shell, "web": list(recordings.get("web") or [])}))
    (folder / NETWORK_LOG).unlink(missing_ok=True)
    return path


def bash_hook(event: dict, recordings_file: Path) -> dict | None:
    """The PreToolUse reply for one Bash call: the recorded answer for an exactly recorded network command,
    else None (no output: Claude Code carries on, and the sandbox refuses any live network)."""
    if event.get("tool_name") != BASH_TOOL:
        return None
    tool_input = event.get("tool_input") or {}
    command = normalise(str(tool_input.get("command", "")))
    if not uses_network(command):
        return None
    recordings = json.loads(Path(recordings_file).read_text()).get("shell") or []
    matches = [recording for recording in recordings if recording["command"] == command]
    log = Path(recordings_file).parent / NETWORK_LOG
    served_before = sum(1 for entry in _read_log(log) if entry["tool"] == BASH_TOOL and entry["served"]
                        and entry["call"] == command)
    _log(log, BASH_TOOL, command, served=bool(matches))
    if not matches:
        return None
    recording = matches[min(served_before, len(matches) - 1)]
    replacement = f"cat {shlex.quote(recording['output_file'])}; exit {recording['exit_code']}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "culprit replay: the recorded result of this exact command",
                                   "updatedInput": {**tool_input, "command": replacement}}}


def _read_log(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _log(log: Path, tool: str, call: str, served: bool) -> None:
    with log.open("a") as out:
        out.write(json.dumps({"tool": tool, "call": call, "served": served}) + "\n")


# --- web-hook: WebFetch and WebSearch are never live in a replay ---
# They run outside the shell sandbox (its allow list does not limit them), so culprit's PreToolUse hook denies
# every call. A call identical to one the episode made (same url and prompt, same query) is denied with a reason
# that carries the recorded result; any other is denied as not recorded. An approximation: the agent sees a
# denial holding the content rather than a normal tool result.

WEB_TOOLS = ("WebFetch", "WebSearch")
WEB_KEYS = {"WebFetch": ("url", "prompt"), "WebSearch": ("query",)}
MAX_WEB_RESULT_CHARS = 20_000
WEB_SERVED = "offline replay; the recorded result of this exact call was: "
WEB_NOT_RECORDED = "offline replay: not recorded"


def _web_key(tool: str, tool_input: dict) -> dict:
    return {key: tool_input.get(key) for key in WEB_KEYS[tool]}


def web_hook(event: dict, recordings_file: Path) -> dict | None:
    """The PreToolUse reply for one WebFetch or WebSearch call: always a denial, carrying the recorded result
    when this exact call was recorded."""
    tool = event.get("tool_name")
    if tool not in WEB_TOOLS:
        return None
    key = _web_key(tool, event.get("tool_input") or {})
    recordings = json.loads(Path(recordings_file).read_text()).get("web") or []
    match = next((r for r in recordings if r["tool"] == tool and r["input"] == key), None)
    _log(Path(recordings_file).parent / NETWORK_LOG, tool, json.dumps(key, sort_keys=True), served=match is not None)
    if match is None:
        return _deny(WEB_NOT_RECORDED)
    text = match["text"]
    if len(text) > MAX_WEB_RESULT_CHARS:
        text = f"{text[:MAX_WEB_RESULT_CHARS]}\n[recorded result cut at {MAX_WEB_RESULT_CHARS} characters]"
    return _deny(WEB_SERVED + text)


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def web_hook_main(args: list[str]) -> None:
    """`web-hook <network-recordings.json>`: deny one WebFetch or WebSearch call, read on stdin."""
    try:
        reply = web_hook(json.loads(sys.stdin.read()), Path(args[0])) or _deny(WEB_NOT_RECORDED)
    except Exception:  # whatever goes wrong, the call is still denied: the web is never live in a replay
        reply = _deny(WEB_NOT_RECORDED)
    sys.stdout.write(json.dumps(reply))


def bash_hook_main(args: list[str]) -> None:
    """`bash-hook <network-recordings.json>`: read one PreToolUse event on stdin, print the reply if any."""
    try:
        reply = bash_hook(json.loads(sys.stdin.read()), Path(args[0]))
    except Exception:  # never break the replay's shell: without a reply the sandbox still refuses the network
        reply = None
    if reply is not None:
        sys.stdout.write(json.dumps(reply))


# --- entry point ---

STAND_INS = {"mcp": mcp_main, "answers": answers_main, "bash-hook": bash_hook_main, "web-hook": web_hook_main}


def main() -> None:
    STAND_INS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
