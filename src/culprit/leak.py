"""The leak check: an oracle may add only what its actor could have known before starting.

A drafted task or instruction that carries a detail found only in the person's later messages would
make the replay pass for the wrong reason, so it is rejected before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from culprit.claude import Ask, as_messages, ask_claude

LEAK_SCHEMA = {
    "type": "object",
    "properties": {"leaks": {"type": "boolean"}, "evidence": {"type": "string"}},
    "required": ["leaks", "evidence"],
}


@dataclass(frozen=True)
class LeakCheck:
    leaks: bool
    evidence: str


def leak_prompt(drafted_text: str, original_prompt: str, follow_ups: list[str]) -> str:
    return f"""A drafted text will be given to a coding agent before it starts a task. Decide whether the draft
leaks the answer.

The original request:
<request>{original_prompt}</request>

What the person said later, after seeing the agent's attempt:
<later>
{as_messages(follow_ups)}
</later>

The draft:
<draft>{drafted_text}</draft>

leaks is true if the draft contains a specific detail that appears only in the later messages and that
the agent could not have known from the original request: a named fix, root cause, file, function,
value or command. Stating the outcome the person wanted is allowed. evidence quotes the leaked detail,
or says "none"."""


def check_leak(drafted_text: str, original_prompt: str, follow_ups: list[str], ask: Ask = ask_claude) -> LeakCheck:
    answer = ask(leak_prompt(drafted_text, original_prompt, follow_ups), LEAK_SCHEMA)
    return LeakCheck(bool(answer["leaks"]), str(answer["evidence"]))
