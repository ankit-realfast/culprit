"""Diagnosing an episode: running variants, building each oracle, the verdict and its statistics, the
cost preview, and diagnosing every queued episode in one batch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field, replace
from itertools import combinations
from math import comb
from pathlib import Path
from typing import Callable, Sequence

from culprit.logs import Episode, EpisodeNotFound, find_episode, follow_up_prompts, tool_uses
from culprit.model import (CALL_CAP_USD, Ask, ClaudeCallFailed, Criterion, Judgement, ask_claude, check_leak,
                           criterion_step, judge_replay, load_criterion)
from culprit.replay import (MEMORY_DIR, RESULT_FILE, ReplaySettings, SetupScriptFailed, baseline_settings,
                            replay_workdir, run_replay, settings_from_json, settings_to_json, target_inside)
from culprit.store import (ORACLE_ORDER, SELFTEST_DIR, Cause, Symptom, confirm_step, display_id, episode_dir,
                           queued_ids, read_json, read_symptom, require_queue, short_id, write_json)


# --- stats: The test that decides whether a variant passes clearly more often than the baseline. ---

def fisher_one_sided(variant_passes: int, variant_runs: int, baseline_passes: int, baseline_runs: int) -> float:
    """P(the variant passes at least this often | both pass at the same rate): the hypergeometric upper tail."""
    total_passes = variant_passes + baseline_passes
    total_runs = variant_runs + baseline_runs
    tail = sum(
        comb(total_passes, k) * comb(total_runs - total_passes, variant_runs - k)
        for k in range(variant_passes, min(total_passes, variant_runs) + 1)
    )
    return tail / comb(total_runs, variant_runs)


# --- experiment: One variant of an episode, replayed N times and judged, inside a spend cap. ---
# The outcome — passes out of runs — is what the verdict compares across variants.

REPLAYS_DIR = "replays"
PRISTINE_DIR = "pristine"
OUTCOME_FILE = "outcome.json"
JUDGEMENT_FILE = "judgement.json"
CALLS_FILE = "calls.json"
SUPERSEDED_DIR = "superseded"  # runs judged against an earlier criterion, kept so their spend still counts
MAX_VOID_RETRIES = 5  # extra attempts per variant when runs fail for reasons outside the agent


def pristine_dir(runs_dir: Path, episode_id: str) -> Path:
    """The episode's restored starting files, which every replay (selftests included) is cloned from."""
    return episode_dir(runs_dir, episode_id) / PRISTINE_DIR


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class EpisodeBudget:
    cap_usd: float
    spent_usd: float = 0.0

    def ensure_room(self, next_cap_usd: float) -> None:
        if self.spent_usd + next_cap_usd > self.cap_usd:
            raise BudgetExhausted(f"the next run could spend ${next_cap_usd:.2f}; ${self.spent_usd:.2f} of "
                                  f"${self.cap_usd:.2f} is already spent")

    def spend(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass(frozen=True)
class VariantOutcome:
    variant: str
    passes: int
    runs: int  # counted runs; void runs are retried and left out
    cost_usd: float
    void: int = 0


@dataclass(frozen=True)
class ReplayEvent:
    """What run_variant tells its progress callback: "start" before a replay, "finish" once it is judged."""
    kind: str  # "start" or "finish"
    variant: str
    attempt: int  # the run folder's number; void runs are retried under the next one
    index: int  # which of the n counted runs this attempt is for, from 1
    n: int
    result: str | None = None  # finish: "pass", "fail" or "void"
    cost_usd: float = 0.0  # finish: what this replay cost
    spent_usd: float = 0.0  # the episode's spend so far, this run and its judgement included
    reused: bool = False  # finish: paid for before a stop and read back from disk
    scope: str = ""  # who is running it when that is not obvious: a planted fault, an episode's short id


Progress = Callable[[ReplayEvent], None]


def scoped(progress: Progress | None, scope: str) -> Progress | None:
    """The same callback, with every event marked as belonging to `scope`."""
    if progress is None:
        return None
    return lambda event: progress(replace(event, scope=f"{scope} / {event.scope}" if event.scope else scope))


def run_variant(variant: str, settings: ReplaySettings, *, n: int, episode: Episode, runs_dir: Path,
                criterion: Criterion, budget: EpisodeBudget, replay=run_replay, judge=judge_replay,
                out_dir: Path | None = None, progress: Progress | None = None) -> VariantOutcome:
    episode_folder = episode_dir(runs_dir, episode.episode_id)
    folder = out_dir or episode_folder
    variant_dir = folder / REPLAYS_DIR / variant
    passes = counted = void = 0
    cost = 0.0
    attempt = 0
    tell = progress or (lambda event: None)
    while counted < n and attempt < n + MAX_VOID_RETRIES:
        run_dir = variant_dir / str(attempt)
        event = ReplayEvent("start", variant, attempt, counted + 1, n, spent_usd=round(budget.spent_usd, 4))
        attempt += 1
        reused = (run_dir / RESULT_FILE).is_file() and (run_dir / JUDGEMENT_FILE).is_file()
        if reused:
            # paid for before a stop: reuse it rather than pay again
            run_cost = float(read_json(run_dir / RESULT_FILE).get("cost_usd") or 0)
            judgement = Judgement(**read_json(run_dir / JUDGEMENT_FILE))
        else:
            budget.ensure_room(settings.max_budget_usd + CALL_CAP_USD)
            tell(event)
            result = replay(settings, pristine_dir(runs_dir, episode.episode_id), run_dir, replay_workdir(episode),
                            criterion.checks, original_cwd=Path(episode.cwd),
                            original_memory=episode.session_file.parent / MEMORY_DIR)
            budget.spend(result.cost_usd)  # charged before judging, so a judge failure cannot lose it
            run_cost = result.cost_usd
            judgement = judge(criterion, result)
            if judgement.by == "model":
                budget.spend(CALL_CAP_USD)
            write_json(run_dir / JUDGEMENT_FILE, asdict(judgement))
        cost += run_cost
        verdict = "void" if judgement.by == "void" else "pass" if judgement.passed else "fail"
        tell(replace(event, kind="finish", result=verdict, cost_usd=run_cost, spent_usd=round(budget.spent_usd, 4),
                     reused=reused))
        if judgement.by == "void":
            void += 1
            continue
        counted += 1
        passes += int(judgement.passed)
    outcome = VariantOutcome(variant, passes, counted, round(cost, 4), void)
    write_json(variant_dir / OUTCOME_FILE, asdict(outcome))
    return outcome


def stored_spend(folder: Path) -> float:
    """Everything an episode (or selftest) folder has already spent: replays, model judgements, other calls."""
    total = 0.0
    run_dirs = [*(folder / REPLAYS_DIR).glob("*/*"), *(folder / SUPERSEDED_DIR).glob(f"*/{REPLAYS_DIR}/*/*")]
    for run_dir in run_dirs:
        total += float(read_json(run_dir / RESULT_FILE, {}).get("cost_usd") or 0)
        if read_json(run_dir / JUDGEMENT_FILE, {}).get("by") == "model":
            total += CALL_CAP_USD
    for calls in [folder / CALLS_FILE, *(folder / SUPERSEDED_DIR).glob(f"*/{CALLS_FILE}")]:
        total += float(read_json(calls, {}).get("usd") or 0)
    return round(total, 4)


def record_call_spend(folder: Path, usd: float) -> None:
    """Charge a drafting or leak-check call to the folder, at its cap (ask_claude does not report its cost)."""
    spent = float(read_json(folder / CALLS_FILE, {}).get("usd") or 0)
    write_json(folder / CALLS_FILE, {"usd": round(spent + usd, 4)})


def load_outcome(folder: Path, variant: str) -> VariantOutcome | None:
    data = read_json(folder / REPLAYS_DIR / variant / OUTCOME_FILE)
    return None if data is None else VariantOutcome(**data)


def load_outcomes(folder: Path) -> tuple[VariantOutcome, ...]:
    """Every variant measured in an episode (or selftest) folder, by variant name."""
    return tuple(VariantOutcome(**read_json(path)) for path in sorted((folder / REPLAYS_DIR).glob(f"*/{OUTCOME_FILE}")))


# --- verdict: The decision rules and the stored verdict: one cause per episode, with the pass rates that prove it. ---

SIGNIFICANCE = 0.05
VERDICT_FILE = "verdict.json"
BASELINE = "baseline"  # the unchanged replay
COMBINED = "combined"  # every tested change at once


def pair_variant(first: str, second: str) -> str:
    """The variant that makes two causes' changes together, e.g. `task+model`."""
    return f"{first}+{second}"


@dataclass(frozen=True)
class Verdict:
    episode_id: str
    symptom: str
    cause: str
    evidence: str
    outcomes: dict[str, dict]
    untested: dict[str, str]
    pair: tuple[str, ...] = ()


def beats(variant: VariantOutcome, baseline: VariantOutcome) -> bool:
    return fisher_one_sided(variant.passes, variant.runs, baseline.passes, baseline.runs) < SIGNIFICANCE


def reproducible(baseline: VariantOutcome) -> bool:
    """The failure reproduces unless the unchanged replay passes most of the time."""
    return baseline.passes * 2 <= baseline.runs


def numbers_line(verdict: Verdict) -> str | None:
    """The pass counts behind a verdict, e.g. `harness: baseline 0/10 → 8/10, p = 0.0004`; None without replays."""
    baseline = verdict.outcomes.get(BASELINE)
    if baseline is None:
        return None
    candidates = ("+".join(verdict.pair), COMBINED) if verdict.cause == "multi-cause" else (verdict.cause,)
    p_of = {name: fisher_one_sided(verdict.outcomes[name]["passes"], verdict.outcomes[name]["runs"],
                                   baseline["passes"], baseline["runs"])
            for name in candidates if name in verdict.outcomes}
    # the variant that beat the baseline; with two changes the pair may have been run and lost to `combined`
    winner = next((name for name, p in p_of.items() if p < SIGNIFICANCE), None)
    label = verdict.cause if winner in (None, verdict.cause) else f"{verdict.cause} ({winner})"
    line = f"{label}: baseline {baseline['passes']}/{baseline['runs']}"
    if winner is None:
        return line
    won, p = verdict.outcomes[winner], p_of[winner]
    return f"{line} → {won['passes']}/{won['runs']}, p {'< 0.0001' if p < 0.0001 else f'= {p:.4f}'}"


def save_verdict(folder: Path, verdict: Verdict) -> Path:
    return write_json(folder / VERDICT_FILE, asdict(verdict))


def load_verdict(folder: Path) -> Verdict | None:
    data = read_json(folder / VERDICT_FILE)
    if data is None:
        return None
    data["pair"] = tuple(data.get("pair") or ())
    return Verdict(**data)


# --- oracles: The oracles: for each cause, the baseline with only that cause's fields made as good as they can be. ---
# A variant that passes where the baseline failed proves its cause. Each oracle changes only the fields
# VARIANT_FIELDS names for it, and a drafted text runs only after the leak check clears it.

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


def agent_instructions_prompt(criterion: Criterion, symptom: Symptom, rejected_evidence: str | None,
                              repository: str = "") -> str:
    return f"""Write standing instructions for a coding agent, the kind a team keeps in CLAUDE.md, that would have
prevented this kind of failure: {SYMPTOM_DESCRIPTIONS[symptom]}.

For context only, success on the current task meant: {criterion.success_means}

{repository}

The instructions must be general working rules — how to scope, verify and report — true for any task in
this repository. They are appended to the instructions above, so fill their gaps rather than repeat what
they already say. They must not mention this task's specifics, files, values or fix. At most 12 lines.{_retry_note(rejected_evidence)}
Return the instructions as text."""


# What the agent knew before the episode began: the instructions Claude Code loads at launch, in its documented
# load order (code.claude.com/docs/en/memory), and the starting layout. CLAUDE.md files in subfolders and
# rules scoped by `paths:` load on demand during the episode, so they are left out.
MAX_INSTRUCTIONS_CHARS = 20_000  # every instruction file the drafter sees, together
MAX_INSTRUCTION_FILE_BYTES = 4 * 1024 * 1024  # Claude Code skips larger files
MAX_IMPORT_DEPTH = 5  # how deep `@path` imports are followed, as Claude Code does
MAX_LAYOUT_ENTRIES = 150
LAYOUT_SKIP = frozenset({".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                         ".pytest_cache", ".ruff_cache", ".tox", ".next", ".DS_Store", ".idea", "dist", "build",
                         "target"})
INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")  # the working folder and each ancestor
FALLBACK_FILE = "AGENTS.md"  # read only where no INSTRUCTION_FILES exist at any level
RULES_DIR = ".claude/rules"
PROJECT_SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
# The replay runs with the user's own config and the machine's policy, so it loads these too. Tests point
# every one at tmp paths.
USER_CLAUDE_DIR = Path.home() / ".claude"
MANAGED_INSTRUCTIONS = (Path("/Library/Application Support/ClaudeCode/CLAUDE.md"), Path("/etc/claude-code/CLAUDE.md"))
MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                    Path("/etc/claude-code/managed-settings.json"))
ANCESTOR_ROOT = Path("/")  # the farthest folder whose instruction files load
IMPORT = re.compile(r"(?:^|(?<=\s))@((?:~|\.{1,2})?/?[^\s`]+)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
PATHS_FRONTMATTER = re.compile(r"\A---\s*\n(?:(?!---).*\n)*?paths\s*:", re.MULTILINE)


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_INSTRUCTION_FILE_BYTES:
            return None
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def glob_matches(path: Path, glob: str) -> bool:
    """Whether a `claudeMdExcludes` glob matches the path: `*` and `?` stay within one folder, `**` crosses."""
    pattern, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            pattern, i = pattern + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            pattern, i = pattern + ".*", i + 2
        else:
            pattern += {"*": "[^/]*", "?": "[^/]"}.get(glob[i], re.escape(glob[i]))
            i += 1
    return re.fullmatch(pattern, str(path)) is not None


def _norm(path: Path) -> Path:
    return Path(os.path.normpath(path))


def existing_instructions(episode: Episode, pristine: Path) -> list[tuple[str, str]]:
    """(source, text) for every instruction Claude Code loads when a replay of this episode starts, in its load
    order: managed policy, the user's CLAUDE.md and rules, then CLAUDE.md files from the root down to the
    working folder, then the project's rules; each followed by the files it imports with `@path`.

    Paths are the original ones; a file under the working folder is read from the restored starting copy,
    anything else from disk. Files a `claudeMdExcludes` glob matches are skipped, except managed policy.
    Together at most MAX_INSTRUCTIONS_CHARS: when the cap bites, files nearer the working folder are kept."""
    cwd = _norm(Path(episode.cwd)) if episode.cwd else None
    root = _norm(ANCESTOR_ROOT)
    user_dir = _norm(USER_CLAUDE_DIR)

    def location(path: Path) -> Path:
        if cwd is not None and path.is_relative_to(cwd):
            return pristine / path.relative_to(cwd)
        return path

    settings = [_read_settings(user_dir / "settings.json")]
    if cwd is not None:
        settings += [_read_settings(location(cwd / name)) for name in PROJECT_SETTINGS_FILES]
    excludes = [glob for data in settings for glob in data.get("claudeMdExcludes") or [] if isinstance(glob, str)]
    seen: set[Path] = set()

    def loaded(path: Path, label: str, depth: int = 0, excludable: bool = True) -> list[tuple[str, str]]:
        if path in seen or (excludable and any(glob_matches(path, glob) for glob in excludes)):
            return []
        if (text := _read_text(location(path))) is None:
            return []
        seen.add(path)
        files = [(label, text)]
        if depth < MAX_IMPORT_DEPTH:
            for target in IMPORT.findall(FENCE.sub("", text)):
                target = target.rstrip(".,;:)")
                imported = _norm(Path(target).expanduser() if target.startswith("~") else path.parent / target)
                files += loaded(imported, f"{imported} (imported by {label})", depth + 1, excludable)
        return files

    def rules(folder: Path, label: str) -> list[tuple[str, str]]:
        found = sorted(location(folder).rglob("*.md")) if location(folder).is_dir() else []
        files: list[tuple[str, str]] = []
        for rule in found:
            path = folder / rule.relative_to(location(folder))
            text = _read_text(rule)
            if text is not None and not PATHS_FRONTMATTER.match(text):  # path-scoped rules load on demand
                files += loaded(path, f"{label}/{rule.relative_to(location(folder))}")
        return files

    folders = [] if cwd is None else [cwd, *(f for f in cwd.parents if f.is_relative_to(root))]
    names = INSTRUCTION_FILES
    if not any(location(folder / name).is_file() for folder in folders for name in INSTRUCTION_FILES):
        names = (FALLBACK_FILE,)
    groups: list[list[tuple[str, str]]] = []  # nearest the working folder first
    for folder in folders:
        level = [f for name in names for f in loaded(folder / name, name if folder == cwd else str(folder / name))]
        if folder == cwd:
            level += rules(cwd / RULES_DIR, RULES_DIR)
        groups.append(level)
    groups.append(loaded(user_dir / "CLAUDE.md", "user: ~/.claude/CLAUDE.md")
                  + rules(user_dir / "rules", "user: ~/.claude/rules"))
    managed = [f for path in MANAGED_INSTRUCTIONS
               for f in loaded(_norm(path), f"managed policy: {path}", excludable=False)]
    for path in MANAGED_SETTINGS:
        text = _read_settings(path).get("claudeMd")
        if isinstance(text, str) and text.strip():
            managed.append((f"managed settings: {path} claudeMd", text))
    groups.append(managed)
    kept: list[list[tuple[str, str]]] = []
    room = MAX_INSTRUCTIONS_CHARS
    for group in groups:
        shown = []
        for source, text in group:
            if room <= 0:
                break
            shown.append((source, text[:room]))
            room -= len(text)
        kept.append(shown)
    return [f for group in reversed(kept) for f in group]


def folder_layout(pristine: Path) -> list[str]:
    """The starting copy's top two levels, folders marked with '/', at most MAX_LAYOUT_ENTRIES lines."""
    def entries(folder: Path) -> list[os.DirEntry]:
        try:
            return sorted((e for e in os.scandir(folder) if e.name not in LAYOUT_SKIP), key=lambda e: e.name)
        except OSError:
            return []

    lines: list[str] = []
    for top in entries(pristine):
        if not top.is_dir(follow_symlinks=False):
            lines.append(top.name)
            continue
        lines.append(f"{top.name}/")
        lines.extend(f"{top.name}/{e.name}{'/' if e.is_dir(follow_symlinks=False) else ''}"
                     for e in entries(Path(top.path)))
    if len(lines) > MAX_LAYOUT_ENTRIES:
        hidden = len(lines) - MAX_LAYOUT_ENTRIES + 1
        lines = lines[:MAX_LAYOUT_ENTRIES - 1] + [f"… {hidden} more not shown"]
    return lines


def repository_context(episode: Episode, pristine: Path) -> str:
    """The standing-instructions drafter's view of the repository as the agent found it."""
    instructions = existing_instructions(episode, pristine)
    if instructions:
        shown = "".join(f'<instructions source="{source}">\n{text.rstrip()}\n</instructions>\n'
                        for source, text in instructions)
        part = f"The agent already loads these instructions, in this order:\n{shown}"
    else:
        part = "The agent loads no CLAUDE.md instructions.\n"
    layout = "\n".join(folder_layout(pristine)) or "(empty)"
    return f"{part}\nThe repository's layout when the task began (top two levels):\n<layout>\n{layout}\n</layout>"


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
        repository = repository_context(episode, pristine)
        return _drafted(base, "append_system_prompt",
                        lambda evidence: agent_instructions_prompt(criterion, symptom, evidence, repository),
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


# --- diagnose: Diagnose one episode: baseline, then each oracle in cause order until one beats the baseline, ---
# then the pair test. Every built variant and every outcome is saved, so a run stopped by the budget
# resumes where it stopped instead of paying again.

VARIANTS_DIR = "variants"
NOT_RUN_REASON = "not run: an earlier cause already explained the failure"
CRITERION_USED_FILE = "criterion_used.json"


def criterion_key(criterion: Criterion) -> str:
    text = json.dumps([criterion.success_means, list(criterion.checks), list(criterion.must_not)])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def criterion_changed(folder: Path, criterion: Criterion) -> bool:
    """True when the folder's runs were judged against a different criterion than this one."""
    stored = read_json(folder / CRITERION_USED_FILE)
    return stored is not None and stored["key"] != criterion_key(criterion)


def variant_file(folder: Path, cause: Cause) -> Path:
    """Where the oracle's built settings (or why it could not be built) are kept."""
    return folder / VARIANTS_DIR / f"{cause.value}.json"


def retire_if_criterion_changed(folder: Path, criterion: Criterion) -> bool:
    """Set aside every run, variant and verdict judged against a different criterion; True if any were.

    They move to superseded/<n>/ rather than being deleted, so their spend still counts against the budget."""
    retired = False
    if criterion_changed(folder, criterion):
        stale = [folder / name for name in (REPLAYS_DIR, VARIANTS_DIR, VERDICT_FILE, CALLS_FILE)
                 if (folder / name).exists()]
        if stale:
            destination = folder / SUPERSEDED_DIR / str(len(list((folder / SUPERSEDED_DIR).glob("*"))) + 1)
            destination.mkdir(parents=True)
            for path in stale:
                shutil.move(str(path), destination / path.name)
            retired = True
    write_json(folder / CRITERION_USED_FILE, {"key": criterion_key(criterion)})
    return retired


def diagnose(episode: Episode, criterion: Criterion, symptom: Symptom, base: ReplaySettings, *, runs_dir: Path,
             follow_ups: list[str], env_script: Path | None, n: int, budget: EpisodeBudget,
             out_dir: Path | None = None, ask=ask_claude, run=run_variant, build=build_variant,
             progress: Progress | None = None) -> Verdict:
    folder = out_dir or episode_dir(runs_dir, episode.episode_id)
    pristine = pristine_dir(runs_dir, episode.episode_id)
    retire_if_criterion_changed(folder, criterion)
    outcomes: dict[str, VariantOutcome] = {}
    untested: dict[str, str] = {}

    def charged_ask(prompt: str, schema: dict) -> dict:
        budget.ensure_room(CALL_CAP_USD)
        budget.spend(CALL_CAP_USD)
        record_call_spend(folder, CALL_CAP_USD)
        return ask(prompt, schema)

    def measured(name: str, settings: ReplaySettings) -> VariantOutcome:
        saved = load_outcome(folder, name)
        result = saved or run(name, settings, n=n, episode=episode, runs_dir=runs_dir, criterion=criterion,
                              budget=budget, out_dir=out_dir, progress=progress)
        outcomes[name] = result
        return result

    def finish(cause: Cause, evidence: str, pair: tuple[str, ...] = ()) -> Verdict:
        verdict = Verdict(episode.episode_id, symptom.value, cause.value, evidence,
                          {name: asdict(o) for name, o in outcomes.items()}, untested, pair)
        save_verdict(folder, verdict)
        return verdict

    if symptom is Symptom.NO_FAILURE:
        return finish(Cause.MISJUDGED, "the human confirmed this candidate was not a failure")

    baseline = measured(BASELINE, base)
    if not reproducible(baseline):
        return finish(Cause.NOT_REPRODUCIBLE, f"the unchanged replay passed {_rate(baseline)}")
    best_possible = VariantOutcome("best possible", n, n, 0.0)
    if baseline.runs == 0 or not beats(best_possible, baseline):
        return finish(Cause.UNEXPLAINED, f"not testable at N={n}: the baseline passed {_rate(baseline)}"
                      f"{f' with {baseline.void} void runs' if baseline.void else ''}, so even {n}/{n} could not "
                      f"beat it")

    built: dict[Cause, ReplaySettings] = {}
    for position, cause in enumerate(ORACLE_ORDER):
        settings = _oracle(folder, cause, base, untested, lambda c=cause: build(
            c, base, episode=episode, follow_ups=follow_ups, criterion=criterion, symptom=symptom,
            pristine=pristine, env_script=env_script, ask=charged_ask))
        if settings is None:
            continue
        built[cause] = settings
        result = measured(cause.value, settings)
        if beats(result, baseline):
            for later in ORACLE_ORDER[position + 1:]:
                untested.setdefault(later.value, NOT_RUN_REASON)
            return finish(cause, f"{cause.value} passed {_rate(result)} against the baseline's {_rate(baseline)}")

    if len(built) < 2:
        return finish(Cause.UNEXPLAINED, "no single change made it pass, and fewer than two changes could be tested")
    together = measured(COMBINED, combine(base, built))
    if not beats(together, baseline):
        return finish(Cause.UNEXPLAINED, f"even every change together passed only {_rate(together)}")
    for first, second in combinations(list(built), 2):
        name = pair_variant(first.value, second.value)
        result = measured(name, combine(base, {first: built[first], second: built[second]}))
        if beats(result, baseline):
            return finish(Cause.MULTI_CAUSE, f"{name} passed {_rate(result)} against the baseline's {_rate(baseline)}",
                          (first.value, second.value))
    return finish(Cause.MULTI_CAUSE, f"only every change together passed ({_rate(together)})",
                  tuple(c.value for c in built))


def _oracle(folder: Path, cause: Cause, base: ReplaySettings, untested: dict[str, str], make) -> ReplaySettings | None:
    path = variant_file(folder, cause)
    data = read_json(path)
    if data is None:
        made = make()
        data = {"untested": made.untested_reason} if made.settings is None else settings_to_json(made.settings)
        write_json(path, data)
    if "untested" in data:
        untested[cause.value] = data["untested"]
        return None
    return settings_from_json(data)


def _rate(outcome: VariantOutcome) -> str:
    return f"{outcome.passes}/{outcome.runs}"


# --- batch: Diagnose every prepared episode in the triage queue, sharing one total budget. ---
# An episode that hits its own cap is set aside and the batch goes on; when the total runs out the batch
# stops and names the next episode, and rerunning resumes, because finished verdicts are skipped.

ENV_FIX_FILE = "env-fix.sh"


@dataclass
class BatchSummary:
    diagnosed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    over_cap: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    spent_usd: float = 0.0


def env_fix_script(folder: Path) -> Path | None:
    """The environment fix stored with `culprit env-fix`, if any."""
    script = folder / ENV_FIX_FILE
    return script if script.exists() else None


def total_spend(runs_dir: Path) -> float:
    """What every episode in runs/ has spent, selftests aside: the batch total covers all of it."""
    if not runs_dir.is_dir():
        return 0.0
    return sum(stored_spend(folder) for folder in runs_dir.iterdir() if folder.is_dir() and folder.name != SELFTEST_DIR)


def readiness_problem(runs_dir: Path, episode_id: str) -> str | None:
    shown = display_id(runs_dir, episode_id)
    symptom = read_symptom(runs_dir, episode_id)
    if symptom is None:
        return f"not confirmed — {confirm_step(runs_dir, episode_id)}"
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None or not criterion.approved:
        return f"no approved criterion — {criterion_step(criterion, shown, symptom.value)}"
    if not pristine_dir(runs_dir, episode_id).is_dir():
        return f"not restored — `culprit diagnose {shown}` restores it or says why it cannot"
    return None


def diagnose_all(runs_dir: Path, projects_dir: Path, *, n: int, per_episode_usd: float, total_usd: float,
                 replay_budget_usd: float, diagnose_fn=diagnose, find=find_episode,
                 follow=follow_up_prompts, progress: Progress | None = None) -> BatchSummary:
    summary = BatchSummary()
    smallest_useful_usd = replay_budget_usd + CALL_CAP_USD  # one replay and its judgement
    require_queue(runs_dir)
    queued = queued_ids(runs_dir)
    # the total covers every batch ever run in this folder, not just this one
    remaining = total_usd - total_spend(runs_dir)
    for episode_id in queued:
        folder = episode_dir(runs_dir, episode_id)
        if load_verdict(folder) is not None:
            summary.skipped[episode_id] = "already diagnosed"
            continue
        problem = readiness_problem(runs_dir, episode_id)
        if problem:
            summary.skipped[episode_id] = problem
            continue
        if remaining < smallest_useful_usd:
            summary.stopped_at = episode_id
            break
        already = stored_spend(folder)
        cap = min(per_episode_usd, already + remaining)
        budget = EpisodeBudget(cap, spent_usd=already)
        stop = False
        try:
            episode = find(projects_dir, episode_id)
            diagnose_fn(episode, load_criterion(runs_dir, episode_id), read_symptom(runs_dir, episode_id),
                        baseline_settings(episode, replay_budget_usd), runs_dir=runs_dir,
                        follow_ups=follow(projects_dir, episode_id),
                        env_script=env_fix_script(folder), n=n, budget=budget,
                        progress=scoped(progress, short_id(episode_id)))
            summary.diagnosed.append(episode_id)
        except BudgetExhausted:
            if cap < per_episode_usd:  # the total ran out, not this episode's own cap
                summary.stopped_at = episode_id
                stop = True
            else:
                summary.over_cap.append(episode_id)
        except (ClaudeCallFailed, SetupScriptFailed, EpisodeNotFound) as error:
            summary.skipped[episode_id] = f"failed: {error}"
        spent_now = budget.spent_usd - already
        summary.spent_usd += spent_now
        remaining -= spent_now
        if stop:
            break
    return summary


# --- preview: The most a replaying command can run and spend, worked out and printed before it starts. ---
# Every count follows the real code path: each variant runs up to n + MAX_VOID_RETRIES replays, each replay is
# capped at its settings' budget plus one judge call, and drafting a task or instructions costs model calls.

CALLS_PER_DRAFT_ATTEMPT = 2  # the draft and its leak check


@dataclass(frozen=True)
class CostPlan:
    replays: int  # most replays the command can start
    calls: int  # most drafting and leak-check calls
    max_usd: float  # most it can add to what is already spent
    spent_usd: float  # already spent, counted against the caps
    cap_usd: float

    def __add__(self, other: CostPlan) -> CostPlan:
        return CostPlan(self.replays + other.replays, self.calls + other.calls,
                        round(self.max_usd + other.max_usd, 2), round(self.spent_usd + other.spent_usd, 4),
                        round(self.cap_usd + other.cap_usd, 2))

    def within(self, room_usd: float) -> CostPlan:
        """The same plan when only room_usd is left to spend overall."""
        return CostPlan(self.replays, self.calls, round(max(0.0, min(self.max_usd, room_usd)), 2), self.spent_usd,
                        self.cap_usd)

    def lines(self) -> list[str]:
        calls = f" and {self.calls} drafting calls" if self.calls else ""
        return [f"plan: up to {self.replays} replays{calls} (each variant: n runs plus up to {MAX_VOID_RETRIES} "
                f"void retries)",
                f"cost: at most ${self.max_usd:.2f} more — ${self.spent_usd + self.max_usd:.2f} in total with the "
                f"${self.spent_usd:.2f} already spent (cap ${self.cap_usd:.2f})"]


EMPTY = CostPlan(0, 0, 0.0, 0.0, 0.0)


def plan_cost(replay_caps: Sequence[float], *, n: int, calls: int = 0, spent_usd: float, cap_usd: float) -> CostPlan:
    """replay_caps holds one replay budget per variant still to run."""
    per_variant = n + MAX_VOID_RETRIES
    worst = sum((cap + CALL_CAP_USD) * per_variant for cap in replay_caps) + calls * CALL_CAP_USD
    return CostPlan(per_variant * len(replay_caps), calls, round(max(0.0, min(cap_usd - spent_usd, worst)), 2),
                    spent_usd, cap_usd)


def diagnose_cost(base_cap: float, oracles: Sequence[Cause], *, n: int, spent_usd: float, cap_usd: float,
                  done: frozenset[str] = frozenset(), drafts: int = 0) -> CostPlan:
    """Baseline, every oracle, and when two or more can run, all of them together and every pair."""
    def cap_for(causes) -> float:
        return base_cap * (HARNESS_BUDGET_MULTIPLIER if Cause.HARNESS in causes else 1)

    caps = {BASELINE: base_cap, **{cause.value: cap_for({cause}) for cause in oracles}}
    if len(oracles) >= 2:
        caps[COMBINED] = cap_for(oracles)
        caps.update({pair_variant(a.value, b.value): cap_for({a, b}) for a, b in combinations(oracles, 2)})
    return plan_cost([cap for name, cap in caps.items() if name not in done], n=n,
                     calls=drafts * MAX_DRAFT_ATTEMPTS * CALLS_PER_DRAFT_ATTEMPT, spent_usd=spent_usd, cap_usd=cap_usd)


def diagnose_cost_for(folder: Path, criterion: Criterion, base_cap: float, *, model: str | None, env_fix: bool,
                      n: int, spent_usd: float, cap_usd: float) -> CostPlan:
    """diagnose_cost for what is on disk: stored outcomes and drafted variants are reused, not paid again."""
    stale = criterion_changed(folder, criterion)
    oracles, drafts = [], 0
    for cause in ORACLE_ORDER:
        data = None if stale else read_json(variant_file(folder, cause))
        if data is not None and "untested" in data:
            continue
        if data is None:
            if cannot_build(cause, model=model, env_fix=env_fix):
                continue
            drafts += cause in DRAFTED_CAUSES
        oracles.append(cause)
    names = [BASELINE, COMBINED, *(c.value for c in oracles),
             *(pair_variant(a.value, b.value) for a, b in combinations(oracles, 2))]
    done = frozenset() if stale else frozenset(name for name in names if load_outcome(folder, name) is not None)
    return diagnose_cost(base_cap, oracles, n=n, spent_usd=spent_usd, cap_usd=cap_usd, done=done, drafts=drafts)


def batch_cost(runs_dir: Path, *, n: int, per_episode_usd: float, total_usd: float,
               replay_budget_usd: float) -> CostPlan:
    """Every queued episode `diagnose` with no id would take on, at worst case (any model), within the shared total."""
    spent = total_spend(runs_dir)
    plan = EMPTY
    for episode_id in queued_ids(runs_dir):
        folder = episode_dir(runs_dir, episode_id)
        if load_verdict(folder) is not None or readiness_problem(runs_dir, episode_id):
            continue
        plan += diagnose_cost_for(folder, load_criterion(runs_dir, episode_id), replay_budget_usd, model=None,
                                  env_fix=env_fix_script(folder) is not None, n=n, spent_usd=stored_spend(folder),
                                  cap_usd=per_episode_usd)
    return CostPlan(plan.replays, plan.calls, round(max(0.0, min(plan.max_usd, total_usd - spent)), 2),
                    round(spent, 4), total_usd)
