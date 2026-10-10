"""The oracles: for each cause, the baseline with only that cause's fields made as good as they can be.

A variant that passes where the baseline failed proves its cause. Each oracle changes only the fields
VARIANT_FIELDS names for it, and a drafted text runs only after the leak check clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from culprit.claude import Ask, ask_claude
from culprit.criterion import Criterion
from culprit.logs import Episode, tool_uses
from culprit.leak import check_leak
from culprit.replay import ReplaySettings, target_inside
from culprit.store import Cause, Symptom

ORACLE_MODEL = "claude-fable-5-1"
FABLE_PREFIX = "claude-fable"
MAX_CONTEXT_CHARS = 100_000
MAX_DRAFT_ATTEMPTS = 2
HARNESS_BUDGET_MULTIPLIER = 2
DRAFTED_CAUSES = (Cause.TASK, Cause.AGENT_SETUP)  # oracles whose text a model drafts
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
REWRITE_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

VARIANT_FIELDS: dict[Cause, tuple[str, ...]] = {
    Cause.TASK: ("prompt",),
    Cause.ENVIRONMENT: ("setup_script",),
    Cause.AGENT_SETUP: ("append_system_prompt",),
    Cause.HARNESS: ("tools", "disallowed_tools", "context_preamble", "max_budget_usd"),
    Cause.MODEL: ("model",),
}

SYMPTOM_DESCRIPTIONS = {
    Symptom.STOPPED_SHORT: "the agent stopped before delivering what was asked",
    Symptom.WRONG_RESULT: "the agent delivered a result that was wrong",
    Symptom.IGNORED_INSTRUCTIONS: "the agent broke a rule or instruction it had been given",
    Symptom.WASTEFUL: "the agent reached the result but wasted time, tokens or effort getting there",
}


@dataclass(frozen=True)
class VariantBuild:
    settings: ReplaySettings | None
    untested_reason: str | None


def _retry_note(rejected_evidence: str | None) -> str:
    """Tells a redraft what the leak check rejected in the last one."""
    if not rejected_evidence:
        return ""
    return f"\nA previous draft was rejected because it leaked this detail: {rejected_evidence}. Leave it out."


def task_rewrite_prompt(episode: Episode, criterion: Criterion, rejected_evidence: str | None) -> str:
    must_not = "; ".join(criterion.must_not) or "nothing listed"
    return f"""Rewrite one request a person gave a coding agent so that it states its scope and success criterion
clearly, keeping the person's intent and voice.

The original request:
<request>{episode.prompt_text}</request>

What success means, approved by the person: {criterion.success_means}
A correct attempt must not: {must_not}

Add only what the agent could legitimately know before starting: the outcome wanted and what is out of
scope. Do not name the fix, the cause, the file to change, or any detail learned later.{_retry_note(rejected_evidence)}
Return the rewritten request as text."""


def agent_instructions_prompt(criterion: Criterion, symptom: Symptom, rejected_evidence: str | None) -> str:
    return f"""Write standing instructions for a coding agent, the kind a team keeps in CLAUDE.md, that would have
prevented this kind of failure: {SYMPTOM_DESCRIPTIONS[symptom]}.

For context only, success on the current task meant: {criterion.success_means}

The instructions must be general working rules — how to scope, verify and report — true for any task in
this repository. They must not mention this task's specifics, files, values or fix. At most 12 lines.{_retry_note(rejected_evidence)}
Return the instructions as text."""


def harness_context(episode: Episode, pristine: Path) -> str:
    cwd = Path(episode.cwd)
    seen: list[str] = []
    for use in tool_uses(episode):
        if use.get("name") in FILE_TOOLS:
            path = (use.get("input") or {}).get("file_path") or (use.get("input") or {}).get("notebook_path")
            if path and path not in seen:
                seen.append(path)
    blocks: list[str] = []
    used = 0
    shown: set[Path] = set()
    for path in seen:
        target = target_inside(cwd, pristine, path)
        if target is None or target in shown or not target.is_file():
            continue
        try:
            text = target.read_text()
        except UnicodeDecodeError:
            continue  # binary files are not context
        block = f"=== {target.relative_to(pristine.resolve())} ===\n{text}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            continue
        shown.add(target)
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    return "These files from the working directory are relevant to the request below:\n\n" + "".join(blocks)


def cannot_build(cause: Cause, *, model: str | None, env_fix: bool) -> str | None:
    """Why this cause's oracle cannot be built for the episode, decided before any model call; None if it can."""
    if cause is Cause.ENVIRONMENT and not env_fix:
        return "no environment fix supplied (culprit env-fix)"
    if cause is Cause.MODEL and (model or "").startswith(FABLE_PREFIX):
        return "the episode already ran on Fable, the strongest model"
    return None


def build_variant(cause: Cause, base: ReplaySettings, *, episode: Episode, follow_ups: list[str],
                  criterion: Criterion, symptom: Symptom, pristine: Path, env_script: Path | None,
                  ask: Ask = ask_claude) -> VariantBuild:
    if reason := cannot_build(cause, model=base.model, env_fix=env_script is not None):
        return VariantBuild(None, reason)
    if cause is Cause.TASK:
        return _drafted(base, "prompt", lambda evidence: task_rewrite_prompt(episode, criterion, evidence),
                        episode, follow_ups, ask)
    if cause is Cause.AGENT_SETUP:
        return _drafted(base, "append_system_prompt",
                        lambda evidence: agent_instructions_prompt(criterion, symptom, evidence),
                        episode, follow_ups, ask)
    if cause is Cause.ENVIRONMENT:
        return VariantBuild(replace(base, setup_script=env_script), None)
    if cause is Cause.HARNESS:
        context = harness_context(episode, pristine) or None
        return VariantBuild(replace(base, tools="default", disallowed_tools=(), context_preamble=context,
                                    max_budget_usd=base.max_budget_usd * HARNESS_BUDGET_MULTIPLIER), None)
    if cause is Cause.MODEL:
        return VariantBuild(replace(base, model=ORACLE_MODEL), None)
    raise ValueError(f"{cause} has no oracle")


def combine(base: ReplaySettings, variants: dict[Cause, ReplaySettings]) -> ReplaySettings:
    changes = {field: getattr(settings, field) for cause, settings in variants.items() for field in VARIANT_FIELDS[cause]}
    return replace(base, **changes)


def _drafted(base: ReplaySettings, field: str, prompt_for, episode: Episode, follow_ups: list[str],
             ask: Ask) -> VariantBuild:
    evidence = None
    for _ in range(MAX_DRAFT_ATTEMPTS):
        text = str(ask(prompt_for(evidence), REWRITE_SCHEMA)["text"]).strip()
        verdict = check_leak(text, episode.prompt_text, follow_ups, ask=ask)
        if not verdict.leaks:
            return VariantBuild(replace(base, **{field: text}), None)
        evidence = verdict.evidence
    return VariantBuild(None, f"the leak check rejected {MAX_DRAFT_ATTEMPTS} drafts (last: {evidence})")
