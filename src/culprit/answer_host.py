"""The replay's permission host: answers AskUserQuestion with what you answered in the original session.

Claude Code offers AskUserQuestion headless only when a permission-prompt tool is configured
(verified live on 2.1.296: a PreToolUse hook is not enough). This MCP tool is that host. A question
close to one you answered gets your recorded answer; anything else is refused, exactly as a replay
with no human present refuses every permission prompt.

Run as: python -m culprit.answer_host <answers.json>
"""

from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher

from culprit.logs import ASK_USER_TOOL
from culprit.mcp_stub import serve_message, serve_stdio, text_result, tool_spec

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


def main() -> None:
    answers = json.loads(open(sys.argv[1]).read())
    tools = [tool_spec(TOOL, "Answers permission prompts during a culprit replay.")]

    def call(name: str, arguments: dict) -> dict:
        return text_result(json.dumps(decide(arguments, answers)))

    serve_stdio(lambda message: serve_message(message, tools, call))


if __name__ == "__main__":
    main()
