"""Every model call culprit makes: the locked-down headless call, the success criterion it drafts, the leak
check and the judge."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from culprit.logs import Episode
from culprit.replay import DIFF_FILE, LIMIT_BUDGET, LIMIT_TIME, NO_MCP_SERVERS, TRANSCRIPT_FILE, ReplayResult
from culprit.store import NotReady, display_id, episode_dir, read_json, write_json


# --- claude: The one place culprit asks a model a question: a locked-down headless Claude call with a JSON schema. ---
# No tools, no MCP servers, no permission prompts, no session log, and a spend cap on every call.

DRAFTING_MODEL = "claude-sonnet-5-5"
CALL_TIMEOUT_S = 600
CALL_CAP_USD = 0.50  # the most one call may spend, and what culprit charges for it (the CLI reports no cost)
# Calls run from their own empty folder, so they never read a project's CLAUDE.md and the empty
# memory folder Claude Code creates for them is easy to recognise under ~/.claude/projects.
CALLS_DIR = Path.home() / ".culprit" / "calls"

Ask = Callable[[str, dict], dict]
# beside a call's structured answer, the model id the call actually ran as (from the CLI's modelUsage), when known
MODEL_RAN_KEY = "_model"


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
    answer = payload["structured_output"]
    if isinstance(answer, dict) and (ran := model_used(payload)):
        answer = {**answer, MODEL_RAN_KEY: ran}
    return answer


def model_used(payload: dict) -> str | None:
    """The model a call's JSON result says did the work: of the models in its modelUsage, the one that wrote most."""
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    return max(usage, key=lambda name: (usage[name] or {}).get("outputTokens") or 0
               if isinstance(usage[name], dict) else 0)


# --- criterion ---
# What success means for one episode: drafted by a model from the human's reaction, approved by the human.
# Every pass/fail judgement culprit makes about a replay is made against this criterion.

CRITERION_FILE = "criterion.json"
CRITERION_SCHEMA = {
    "type": "object",
    "properties": {
        "success_means": {"type": "string"},
        "checks": {"type": "array", "items": {"type": "string"}},
        "must_not": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["success_means", "checks", "must_not"],
}


@dataclass(frozen=True)
class Criterion:
    success_means: str
    checks: tuple[str, ...]
    must_not: tuple[str, ...]
    approved: bool = False
    model: str | None = None  # the model that drafted it, when the call reported one


def drafting_prompt(episode: Episode, follow_ups: list[str]) -> str:
    return f"""You are writing the success criterion for one request a person made to a coding agent.
A later step will re-run the agent on this request and judge each attempt against what you write.

The request:
<request>{episode.prompt_text}</request>

What the person typed next. Their reaction reveals what they actually wanted:
<follow_ups>
{as_messages(follow_ups)}
</follow_ups>

Working directory: {episode.cwd}

Write three things:
- success_means: one or two sentences stating the observable outcome a correct attempt produces.
- checks: shell commands, run in the working directory after the attempt, that exit 0 only when the
  attempt succeeded. Leave the list empty unless such a command can be written reliably.
- must_not: things a correct attempt must not do.
Describe the outcome, never the implementation: do not name the fix, the file to change, or the code."""


def draft_criterion(episode: Episode, follow_ups: list[str], ask: Ask = ask_claude) -> Criterion:
    answer = ask(drafting_prompt(episode, follow_ups), CRITERION_SCHEMA)
    return Criterion(answer["success_means"], tuple(answer["checks"]), tuple(answer["must_not"]), approved=False,
                     model=answer.get(MODEL_RAN_KEY))


def save_criterion(runs_dir: Path, episode_id: str, criterion: Criterion) -> Path:
    return write_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE, asdict(criterion))


def load_criterion(runs_dir: Path, episode_id: str) -> Criterion | None:
    data = read_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE)
    if data is None:
        return None
    return Criterion(data["success_means"], tuple(data["checks"]), tuple(data["must_not"]), bool(data["approved"]),
                     data.get("model"))


def criterion_step(criterion: Criterion | None, episode_id: str, symptom: str | None) -> str:
    """The command that gets this episode an approved criterion: `confirm` drafts it, and confirming the same
    symptom again approves the draft. Never suggests redrafting over a draft."""
    confirm = f"`culprit confirm {episode_id} {symptom or '<symptom>'}`"
    if criterion is None:
        return f"run {confirm} to draft what success means"
    return f"review it with `culprit status {episode_id}`, then rerun {confirm} to approve it"


def approve_criterion(runs_dir: Path, episode_id: str) -> Criterion:
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None:
        shown = display_id(runs_dir, episode_id)
        raise NotReady(f"no criterion for {shown} — run `culprit confirm {shown} <symptom>` first")
    approved = replace(criterion, approved=True)
    save_criterion(runs_dir, episode_id, approved)
    return approved


# --- leak: The leak check: an oracle may add only what its actor could have known before starting. ---
# A drafted task or instruction that carries a detail found only in the person's later messages would
# make the replay pass for the wrong reason, so it is rejected before it runs.

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


# --- judge: Pass or fail for one replay, against the approved criterion. ---
# A failing check or an errored run fails on the spot; only otherwise does a model read the run's final
# answer and diff. Every judgement says which of the three decided it.

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
    model: str | None = None  # by "model": the model that judged, when the call reported one


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


LIMIT_WORDS = {LIMIT_BUDGET: "spend limit", LIMIT_TIME: "time limit"}


def _limit_note(replay: ReplayResult) -> str:
    """A run stopped at a limit is judged like any other; the judge is told it was cut off."""
    if not replay.limit:
        return ""
    return (f"The attempt was stopped when it reached its {LIMIT_WORDS.get(replay.limit, replay.limit)}; judge "
            f"what it had done by then.\n\n")


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

{_limit_note(replay)}What the agent did, every tool call in order ({replay.num_turns} turns, ${replay.cost_usd:.2f}):
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
    if replay.is_error and not replay.limit:
        return Judgement(False, f"the replay ended in an error: {replay.result_text[:300]}", "replay")
    diff = (replay.run_dir / DIFF_FILE).read_text(errors="replace")
    transcript_path = replay.run_dir / TRANSCRIPT_FILE
    actions = actions_from_transcript(transcript_path.read_text(errors="replace")) if transcript_path.exists() else ""
    answer = ask(judging_prompt(criterion, replay, diff, actions), JUDGEMENT_SCHEMA)
    return Judgement(bool(answer["passed"]), str(answer["reason"]), "model", answer.get(MODEL_RAN_KEY))
