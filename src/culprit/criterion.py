"""What success means for one episode: drafted by a model from the human's reaction, approved by the human.

Every pass/fail judgement culprit makes about a replay is made against this criterion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from culprit.claude import Ask, as_messages, ask_claude
from culprit.logs import Episode
from culprit.store import NotReady, display_id, episode_dir, read_json, write_json

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
    return Criterion(answer["success_means"], tuple(answer["checks"]), tuple(answer["must_not"]), approved=False)


def save_criterion(runs_dir: Path, episode_id: str, criterion: Criterion) -> Path:
    return write_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE, asdict(criterion))


def load_criterion(runs_dir: Path, episode_id: str) -> Criterion | None:
    data = read_json(episode_dir(runs_dir, episode_id) / CRITERION_FILE)
    if data is None:
        return None
    return Criterion(data["success_means"], tuple(data["checks"]), tuple(data["must_not"]), bool(data["approved"]))


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
