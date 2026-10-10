"""Pass or fail for one replay, against the approved criterion.

A failing check or an errored run fails on the spot; only otherwise does a model read the run's final
answer and diff. Every judgement says which of the three decided it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from culprit.claude import Ask, ask_claude
from culprit.criterion import Criterion
from culprit.replay import DIFF_FILE, TRANSCRIPT_FILE, ReplayResult

MAX_DIFF_CHARS = 60_000
MAX_ACTIONS_CHARS = 20_000
TOOL_INPUT_CHARS = 300
JUDGEMENT_SCHEMA = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["passed", "reason"],
}


@dataclass(frozen=True)
class Judgement:
    passed: bool
    reason: str
    by: str  # "check", "replay", "model", or "void" (not counted)


def actions_from_transcript(transcript: str) -> str:
    lines = []
    for raw in transcript.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                lines.append(f"- {block.get('name')}: {json.dumps(block.get('input'))[:TOOL_INPUT_CHARS]}")
    text = "\n".join(lines)
    if len(text) > MAX_ACTIONS_CHARS:
        text = text[:MAX_ACTIONS_CHARS] + f"\n[{len(text) - MAX_ACTIONS_CHARS} more characters of tool calls cut]"
    return text


def judging_prompt(criterion: Criterion, replay: ReplayResult, diff: str, actions: str) -> str:
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff truncated: {len(diff) - MAX_DIFF_CHARS} more characters]"
    must_not = "\n".join(f"- {rule}" for rule in criterion.must_not) or "- (nothing listed)"
    checks = "\n".join(f"- {c.command}: exit {c.exit_code}" for c in replay.checks) or "- (no checks)"
    return f"""Decide whether one attempt by a coding agent met its success criterion.

Success means: {criterion.success_means}

A correct attempt must not:
{must_not}

Checks run after the attempt:
{checks}

The agent's final answer:
<answer>{replay.result_text}</answer>

What the agent did, every tool call in order ({replay.num_turns} turns, ${replay.cost_usd:.2f}):
<actions>
{actions or "(no tool calls)"}
</actions>

Every file change the attempt made:
<diff>
{diff}
</diff>

Judge only against the criterion. passed is true only if the outcome is met and nothing forbidden
was done, in the files or in the actions. reason is one sentence naming the evidence."""


def judge_replay(criterion: Criterion, replay: ReplayResult, ask: Ask = ask_claude) -> Judgement:
    if replay.void_reason:
        return Judgement(False, f"not counted: {replay.void_reason}", "void")
    failed = [check for check in replay.checks if check.exit_code != 0]
    if failed:
        return Judgement(False, f"check failed: {failed[0].command} (exit {failed[0].exit_code})", "check")
    if replay.is_error:
        return Judgement(False, f"the replay ended in an error: {replay.result_text[:300]}", "replay")
    diff = (replay.run_dir / DIFF_FILE).read_text(errors="replace")
    transcript_path = replay.run_dir / TRANSCRIPT_FILE
    actions = actions_from_transcript(transcript_path.read_text(errors="replace")) if transcript_path.exists() else ""
    answer = ask(judging_prompt(criterion, replay, diff, actions), JUDGEMENT_SCHEMA)
    return Judgement(bool(answer["passed"]), str(answer["reason"]), "model")
