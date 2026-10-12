"""Every model call culprit makes: the locked-down headless call, the success criterion it drafts, the leak
check and the judge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from culprit.logs import Episode, episode_cost_usd, signals_for, tool_uses, what_happened
from culprit.replay import (DIFF_FILE, LIMIT_BUDGET, LIMIT_TIME, NO_MCP_SERVERS, TRANSCRIPT_FILE, ReplayResult,
                            ran_as)
from culprit.store import SYMPTOM_MEANINGS, NotReady, Symptom, display_id, episode_dir, read_json, write_json


# --- claude: The one place culprit asks a model a question: a locked-down headless Claude call with a JSON schema. ---
# No tools, no MCP servers, no permission prompts, no session log, and a spend cap on every call.

# Models are named by alias, which Claude Code resolves to the latest model of that tier; an environment variable
# pins another (read on every call, so it can change between runs)
DRAFTING_MODEL = "sonnet"
DRAFTING_MODEL_ENV = "CULPRIT_DRAFTING_MODEL"
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


def drafting_model() -> str:
    """The model that drafts, leak-checks and judges: $CULPRIT_DRAFTING_MODEL, else the sonnet alias."""
    return os.environ.get(DRAFTING_MODEL_ENV) or DRAFTING_MODEL


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


def ask_claude(prompt: str, schema: dict, model: str | None = None, max_budget_usd: float = CALL_CAP_USD,
               run=subprocess.run, cwd: Path = CALLS_DIR) -> dict:
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        completed = run(structured_call_command(prompt, schema, model or drafting_model(), max_budget_usd),
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


# --- resolve: What an alias means today. ---
# `--model fable` runs the latest Fable, so deciding whether an episode already ran on the strongest model needs the
# id the alias stands for. A local slash command answers that for free: Claude Code handles `/cost` without calling
# a model, yet its stream-json init event still names the resolved model (verified 2026-10-11 on 2.1.296: fable →
# claude-fable-5-1, total_cost_usd 0, num_turns 0). Should a build stop doing that, one trivial call capped at
# RESOLVE_CAP_USD reads the same event. The answer is kept in the store for a day.

MODELS_FILE = "models.json"  # in the store: each alias's resolved id and when it was resolved
RESOLVE_MAX_AGE_S = 24 * 3600
RESOLVE_CAP_USD = 0.05
RESOLVE_TIMEOUT_S = 120
FREE_PROBE = "/cost"
PAID_PROBE = "Reply with the single word OK."


KNOWN_FAMILIES = ("fable", "opus", "sonnet", "haiku")  # each is also the alias of its latest model


def model_family(model: str | None) -> str | None:
    """The family a model id or alias names: `claude-opus-4-8` → opus, `fable` → fable, the older
    `claude-3-5-sonnet-20241022` → sonnet, `claude-mythos-1` → mythos (a family culprit does not know); None for
    anything else (`<synthetic>`, no model)."""
    if not model:
        return None
    if model in KNOWN_FAMILIES:
        return model
    if match := re.match(r"claude-([a-z]+)-\d", model):
        return match.group(1)
    known = [part for part in model.split("-") if part in KNOWN_FAMILIES]
    return known[0] if model.startswith("claude-") and known else None


def unknown_models(models) -> list[str]:
    """The model ids, among those given, whose family culprit does not know (sorted, each once)."""
    return sorted({m for m in models if model_family(m) and model_family(m) not in KNOWN_FAMILIES})


@dataclass(frozen=True)
class Resolution:
    model: str | None  # the id the alias resolved to; None when it could not be resolved
    usd: float  # what resolving it spent


def resolve_command(alias: str) -> list[str]:
    return [os.environ.get("CULPRIT_CLAUDE_BIN", "claude"), "-p",
            "--model", alias,
            "--output-format", "stream-json", "--verbose",
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", NO_MCP_SERVERS,
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--max-budget-usd", f"{RESOLVE_CAP_USD:.2f}"]


def _probe(alias: str, prompt: str, run, cwd: Path) -> tuple[str | None, float | None]:
    """The model the probe's init event names and what its result line says it cost (None when unknown)."""
    try:
        completed = run(resolve_command(alias), input=prompt, capture_output=True, text=True,
                        timeout=RESOLVE_TIMEOUT_S, cwd=cwd)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    cost = None
    for line in completed.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result" and record.get("total_cost_usd") is not None:
            cost = float(record["total_cost_usd"])
    return ran_as(completed.stdout), cost


def resolve_alias(alias: str, run=subprocess.run, cwd: Path = CALLS_DIR) -> Resolution:
    """What `--model <alias>` runs as today: free through a local command, else one capped trivial call."""
    cwd.mkdir(parents=True, exist_ok=True)
    model, cost = _probe(alias, FREE_PROBE, run, cwd)
    spent = cost or 0.0
    if model:
        return Resolution(model, spent)
    model, cost = _probe(alias, PAID_PROBE, run, cwd)
    return Resolution(model, round(spent + (cost if cost is not None else RESOLVE_CAP_USD), 4))


def cached_model(runs_dir: Path, alias: str, now: float | None = None) -> str | None:
    """The alias's resolved id if the store resolved it within the last day; never calls anything."""
    entry = read_json(runs_dir / MODELS_FILE, {}).get(alias) or {}
    fresh = (now if now is not None else time.time()) - float(entry.get("at") or 0) < RESOLVE_MAX_AGE_S
    return entry.get("model") if fresh else None


def resolved_model(runs_dir: Path, alias: str, now: float | None = None) -> Resolution:
    """The alias's resolved id: from the store when resolved within a day, else resolved now and stored."""
    now = now if now is not None else time.time()
    if (cached := cached_model(runs_dir, alias, now)) is not None:
        return Resolution(cached, 0.0)
    resolution = resolve_alias(alias)  # looked up when called, so tests can stand a fake in
    if resolution.model:
        stored = read_json(runs_dir / MODELS_FILE, {})
        stored[alias] = {"model": resolution.model, "at": now,
                         "resolved_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}
        write_json(runs_dir / MODELS_FILE, stored)
    return resolution


# --- criterion ---
# What success means for one episode: drafted by a model from the human's reaction, approved by the human.
# Every pass/fail judgement culprit makes about a replay is made against this criterion.

CRITERION_FILE = "criterion.json"
LIMITS_SCHEMA = {
    "type": "object",
    "properties": {"max_tool_calls": {"type": "integer"}, "max_minutes": {"type": "number"},
                   "max_usd": {"type": "number"}},
}


def criterion_schema(symptom: Symptom | None = None) -> dict:
    """What the drafter answers. Limits are optional, except for a wasteful episode, where the outcome alone
    cannot tell a lean attempt from a wasteful one: it must set a tool-call and a time limit and say why."""
    wasteful = symptom == Symptom.WASTEFUL
    limits = {**LIMITS_SCHEMA, **({"required": ["max_tool_calls", "max_minutes"]} if wasteful else {})}
    return {
        "type": "object",
        "properties": {
            "success_means": {"type": "string"},
            "checks": {"type": "array", "items": {"type": "string"}},
            "must_not": {"type": "array", "items": {"type": "string"}},
            "limits": limits,
            "limits_reason": {"type": "string"},
        },
        "required": ["success_means", "checks", "must_not", *(["limits", "limits_reason"] if wasteful else [])],
    }


CRITERION_SCHEMA = criterion_schema()


@dataclass(frozen=True)
class Limits:
    """The most a replay may take before it fails, checked from the run without a model."""
    max_tool_calls: int | None = None
    max_minutes: float | None = None
    max_usd: float | None = None


@dataclass(frozen=True)
class Criterion:
    success_means: str
    checks: tuple[str, ...]
    must_not: tuple[str, ...]
    approved: bool = False
    model: str | None = None  # the model that drafted it, when the call reported one
    limits: Limits | None = None
    limits_reason: str = ""
    symptom: str | None = None  # the symptom it was drafted for; None when drafted before drafts were tagged
    drafted_hash: str | None = None  # content_hash as drafted: a different hash now means the person edited it


def content_hash(criterion: Criterion) -> str:
    """A hash of what the criterion says (not whether it is approved, who drafted it or what for)."""
    text = json.dumps([criterion.success_means, list(criterion.checks), list(criterion.must_not),
                       asdict(criterion.limits) if criterion.limits else None, criterion.limits_reason])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Effort:
    """What the original attempt took, as far as its log tells."""
    tool_calls: int
    minutes: float  # 0 when the log does not say
    usd: float | None  # None when the log does not say


def original_effort(episode: Episode) -> Effort:
    return Effort(len(tool_uses(episode)), round(signals_for(episode).duration_s / 60, 1), episode_cost_usd(episode))


def _number(value: float) -> str:
    return f"{value:g}"


def effort_text(effort: Effort) -> str:
    minutes = f"{_number(effort.minutes)} minutes" if effort.minutes else "time unknown"
    cost = f"${effort.usd:.2f}" if effort.usd is not None else "cost unknown"
    return f"{effort.tool_calls} tool calls, {minutes}, {cost}"


SYMPTOM_GUIDANCE = {
    Symptom.STOPPED_SHORT: "The agent quit before finishing. success_means must name every part of the request "
                           "that must be finished, so an attempt that does only some of them fails.",
    Symptom.WRONG_RESULT: "The agent finished but the result was wrong. success_means states the correct outcome.",
    Symptom.IGNORED_INSTRUCTIONS: "The agent did not follow what the person said. Write each instruction the agent "
                                  "ignored as its own explicit line: what it must do goes in success_means, what it "
                                  "must not do goes in must_not.",
    Symptom.WASTEFUL: "The agent got there, but spent far too much doing it, so the outcome alone cannot catch the "
                      "failure. Also set limits: max_tool_calls and max_minutes (max_usd only when the original's "
                      "cost is known), clearly tighter than what the original attempt took (about half), and "
                      "limits_reason: one line saying why. An attempt that goes over any limit fails.",
    Symptom.NO_FAILURE: "The person says nothing went wrong. success_means states the outcome the attempt reached.",
}


def drafting_prompt(episode: Episode, follow_ups: list[str], symptom: Symptom | None = None) -> str:
    facts = what_happened(episode, follow_ups[0] if follow_ups else None).as_text()
    if symptom is None:
        what_went_wrong = ""
    else:
        what_went_wrong = f"""
What went wrong, as the person confirmed it: {symptom.value} ({SYMPTOM_MEANINGS[symptom]}).
{SYMPTOM_GUIDANCE[symptom]}
"""
    limits_line = ("" if symptom == Symptom.WASTEFUL else
                   "\n- limits: leave them out unless the person's reaction makes a limit part of success.")
    return f"""You are writing the success criterion for one request a person made to a coding agent.
A later step will re-run the agent on this request and judge each attempt against what you write.

The request:
<request>{episode.prompt_text}</request>

What the person typed next. Their reaction reveals what they actually wanted:
<follow_ups>
{as_messages(follow_ups)}
</follow_ups>

What happened in the original attempt:
<facts>
{facts}
</facts>
What the original attempt took: {effort_text(original_effort(episode))}.
{what_went_wrong}
Working directory: {episode.cwd}

Write these:
- success_means: one or two sentences stating the observable outcome a correct attempt produces.
- checks: shell commands, run in the working directory after the attempt, that exit 0 only when the
  attempt succeeded. Leave the list empty unless such a command can be written reliably.
- must_not: things a correct attempt must not do.{limits_line}
Describe the outcome, never the implementation: do not name the fix, the file to change, or the code."""


def _limits(answer: dict) -> Limits | None:
    """The limits a draft set, keeping only positive numbers; None when it set none."""
    raw = answer.get("limits") if isinstance(answer.get("limits"), dict) else {}

    def positive(key: str, kind):
        value = raw.get(key)
        return kind(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None

    limits = Limits(positive("max_tool_calls", int), positive("max_minutes", float), positive("max_usd", float))
    return None if limits == Limits() else limits


def half_of(effort: Effort) -> tuple[Limits | None, str]:
    """A wasteful draft's limits when the drafter set none: half of what the original took, where known."""
    limits = Limits(max(1, effort.tool_calls // 2) if effort.tool_calls else None,
                    round(effort.minutes / 2, 1) if effort.minutes else None)
    if limits == Limits():
        return None, ""
    return limits, f"half of what the original took ({effort_text(effort)}); the draft set no limits"


def draft_criterion(episode: Episode, follow_ups: list[str], symptom: Symptom | None = None,
                    ask: Ask = ask_claude) -> Criterion:
    answer = ask(drafting_prompt(episode, follow_ups, symptom), criterion_schema(symptom))
    limits, reason = _limits(answer), " ".join(str(answer.get("limits_reason") or "").split())
    if limits is None and symptom == Symptom.WASTEFUL:
        limits, reason = half_of(original_effort(episode))
    criterion = Criterion(answer["success_means"], tuple(answer["checks"]), tuple(answer["must_not"]),
                          approved=False, model=answer.get(MODEL_RAN_KEY), limits=limits,
                          limits_reason=reason if limits else "", symptom=symptom.value if symptom else None)
    return replace(criterion, drafted_hash=content_hash(criterion))


def save_criterion(runs_dir: Path, episode_id: str, criterion: Criterion) -> Path:
    return write_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE, asdict(criterion))


def load_criterion(runs_dir: Path, episode_id: str) -> Criterion | None:
    data = read_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE)
    if data is None:
        return None
    limits = data.get("limits")
    return Criterion(data["success_means"], tuple(data["checks"]), tuple(data["must_not"]), bool(data["approved"]),
                     data.get("model"), _limits({"limits": limits}),
                     data.get("limits_reason") or "", data.get("symptom"), data.get("drafted_hash"))


def criterion_step(criterion: Criterion | None, episode_id: str, symptom: str | None) -> str:
    """The command that gets this episode an approved criterion: `confirm` drafts it, and
    `confirm <episode> approve` approves the draft. Never suggests redrafting over a draft."""
    confirm = f"`culprit confirm {episode_id} {symptom or '<symptom>'}`"
    if criterion is None:
        return f"run {confirm} to draft what success means"
    return (f"review it with `culprit status {episode_id}`, then approve it with "
            f"`culprit confirm {episode_id} approve`")


def approve_criterion(runs_dir: Path, episode_id: str) -> Criterion:
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None:
        shown = display_id(runs_dir, episode_id)
        raise NotReady(f"no criterion for {shown} — run `culprit confirm {shown} <symptom>` first")
    approved = replace(criterion, approved=True)
    save_criterion(runs_dir, episode_id, approved)
    return approved


# --- suggestion: Which symptom the What happened facts point to, for the person to accept or overrule. ---
# Shown the facts only (never the criterion or the person's later messages beyond the next prompt the facts
# quote), so the person can check the reason against the block printed above it.

SUGGESTION_FILE = "suggestion.json"
SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {"symptom": {"type": "string", "enum": [s.value for s in Symptom]},
                   "reason": {"type": "string"}},
    "required": ["symptom", "reason"],
}


@dataclass(frozen=True)
class Suggestion:
    symptom: Symptom
    reason: str
    model: str | None = None


def suggestion_prompt(facts: str) -> str:
    symptoms = "\n".join(f"- {symptom.value}: {meaning}" for symptom, meaning in SYMPTOM_MEANINGS.items())
    return f"""A person asked a coding agent for something. Below are the facts of what happened, taken from the
session log. Suggest which one symptom describes it best; the person will accept or overrule you.

<facts>
{facts}
</facts>

The symptoms:
{symptoms}

Pick no-failure only when nothing in the facts points to a problem. A next message that repeats or corrects
the request usually means the attempt did not work.

reason: one line, under 25 words, that cites a specific fact above (a number, a file name, or a short quote),
so the person can check it."""


def suggest_symptom(facts: str, ask: Ask = ask_claude) -> Suggestion:
    """One call; raises ClaudeCallFailed when the answer names no known symptom or gives no reason."""
    answer = ask(suggestion_prompt(facts), SUGGESTION_SCHEMA)
    symptom, reason = answer.get("symptom"), answer.get("reason")
    if symptom not in {s.value for s in Symptom} or not isinstance(reason, str) or not reason.strip():
        raise ClaudeCallFailed("the suggestion named no known symptom or gave no reason")
    return Suggestion(Symptom(symptom), " ".join(reason.split()), answer.get(MODEL_RAN_KEY))


def save_suggestion(runs_dir: Path, episode_id: str, suggestion: Suggestion) -> Path:
    return write_json(episode_dir(runs_dir, episode_id) / SUGGESTION_FILE, asdict(suggestion))


def load_suggestion(runs_dir: Path, episode_id: str) -> Suggestion | None:
    data = read_json(episode_dir(runs_dir, episode_id) / SUGGESTION_FILE)
    return None if data is None else Suggestion(Symptom(data["symptom"]), data["reason"], data.get("model"))


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
