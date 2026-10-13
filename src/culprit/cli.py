"""The culprit command line, and the progress lines long-running commands print."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable, Sequence, TextIO

from culprit import logs, replay
from culprit.diagnose import (BASELINE, DRAFTED_CAUSES, ENV_FIX_FILE, BudgetExhausted, CostPlan, EpisodeBudget,
                              ReplayEvent, VariantOutcome, batch_cost, diagnose, diagnose_all, diagnose_cost,
                              diagnose_cost_for, env_fix_script, known_oracle_id, limit_tally, load_outcome,
                              load_verdict, numbers_line, plan_cost, tokens_per_run, usd_per_run,
                              oracle_model, pristine_dir, record_call_spend, retire_if_criterion_changed, run_variant,
                              stored_spend, unknown_model_warning)
from culprit.logs import (Episode, EpisodeNotFound, FailureSignals, WhatHappened, find_episode, follow_up_prompts,
                          load_snapshot, project_hooks, rank_candidates, replay_blockers, session_files, session_root,
                          summarize_session, what_happened)
from culprit.model import (CALL_CAP_USD, CRITERION_FILE, LIMIT_WORDS, ClaudeCallFailed, Criterion, Suggestion,
                           approve_criterion, ask_claude, criterion_edited, draft_criterion, load_criterion, load_suggestion,
                           resolved_model, save_criterion, save_suggestion, suggest_symptom)
from culprit.replay import (REPLAY_BUDGET_FLOOR_USD, OriginalModified, SetupScriptFailed, apply_episode_originals,
                            baseline_settings, check_restore, episode_originals, restore, restore_from_git)
from culprit.report import (DONE, REPORT_FILE, EpisodeStatus, Step, block, collect, criterion_block, episode_header,
                            episode_status, episode_steps, next_section, progress_lines, queue_status,
                            numbered, queue_steps, render_report, row_command, short_path, shown_runs, trust_block,
                            suggestion_lines, symptoms_block, verdict_lines, what_happened_block, wrap)
from culprit.store import (ORACLE_ORDER, RESTORE_FILE, Cause, NotReady, Symptom, UnknownEpisode,
                           display_id, episode_dir, episode_ref, prompt_excerpt, queued_ids, read_symptom,
                           record_symptom, require_queue, resolve_episode_id, save_candidates, write_json)
from culprit.trust import (MIN_KAPPA, MIN_LABELS, UnknownRun, agreement, control_min_passes, is_control,
                           next_unlabelled, record_label, run_id, run_selftest, run_summary, selftest_accuracy,
                           selftest_cost)


# --- progress: What the CLI shows while replays run: one stderr line per finished replay, and a heartbeat between. ---

HEARTBEAT_S = 30


class Heartbeat:
    """Calls beat() every interval seconds on a daemon thread until stop().

    `wait(interval)` returns True to stop; the default waits on an event, so stop() wakes it at once.
    """

    def __init__(self, beat: Callable[[], None], interval: float,
                 wait: Callable[[float], bool] | None = None):
        self._stopped = threading.Event()
        self._beat, self._interval = beat, interval
        self._wait = wait or self._stopped.wait
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._wait(self._interval):
            self._beat()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()


def elapsed(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def in_scope(event: ReplayEvent, text: str) -> str:
    """text prefixed with the event's scope (planted fault, episode) when there is one."""
    return f"{event.scope}: {text}" if event.scope else text


def describe(event: ReplayEvent) -> str:
    """`baseline #2 of 5`, in its scope."""
    return in_scope(event, f"{event.variant} #{event.index} of {event.n}")


class ProgressPrinter:
    """The progress callback the CLI hands to run_variant, diagnose, run_selftest and diagnose_all.

    Replays run side by side, so events come from several threads; one heartbeat names every replay still
    running. Use it as a context manager so a heartbeat never outlives the command.
    """

    def __init__(self, stream: TextIO | None = None, *, interval: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.monotonic, heartbeat=Heartbeat):
        self.stream = stream or sys.stderr
        self.interval, self.clock, self.heartbeat = interval, clock, heartbeat
        self._beating = None
        self._running: dict[tuple[str, str, int], tuple[float, ReplayEvent]] = {}  # (scope, variant, attempt)
        self._lock = threading.RLock()

    def __enter__(self) -> ProgressPrinter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._running.clear()
            self._stop_beating()

    def _stop_beating(self) -> None:
        if self._beating is not None:
            self._beating.stop()
            self._beating = None

    def __call__(self, event: ReplayEvent) -> None:
        with self._lock:
            key = (event.scope, event.variant, event.attempt)
            if event.kind == "start":
                # runs of another variant or episode left behind raised and never sent their finish
                for other in [k for k in self._running if k[:2] != key[:2]]:
                    del self._running[other]
                self._running[key] = (self.clock(), event)
                if self._beating is None:
                    self._beating = self.heartbeat(self._beat, self.interval)
                return
            self._running.pop(key, None)
            if not self._running:
                self._stop_beating()
            result = "void, not counted" if event.result == "void" else event.result
            if event.limit and event.result != "void":
                result += f" (stopped at its {LIMIT_WORDS.get(event.limit, event.limit)})"
            reused = " — reused from an earlier run" if event.reused else ""
            self.print(f"{describe(event)}: {result}, ${event.cost_usd:.2f} (episode spent ${event.spent_usd:.2f})"
                       f"{reused}")

    def _beat(self) -> None:
        with self._lock:
            if not self._running:
                return
            running = sorted(self._running.values(), key=lambda item: item[1].index)
            first = running[0][1]
            oldest = min(started for started, _ in running)
            numbers = ", ".join(f"#{event.index}" for _, event in running)
            self.print(f"… still running {in_scope(first, first.variant)} {numbers}, {elapsed(self.clock() - oldest)}")

    def print(self, line: str) -> None:
        sys.stdout.flush()  # what stdout already holds shows first where both streams reach one terminal
        print(line, file=self.stream, flush=True)


# --- cli: The culprit command line. Each subcommand is one handler: handler(args) -> exit code. ---

RUNS_ENV = "CULPRIT_RUNS"
PROJECTS_ENV = "CULPRIT_PROJECTS"
EXIT_REFUSED = 1
EXIT_NOT_READY = 3
EXIT_BUDGET = 4
EXIT_INTERRUPTED = 130
RUNS_PER_VARIANT = 10  # N: the baseline and each variant are replayed this many times
# Built-in spend caps, a safety net rather than a limit: each counts what the episode (or, for the total, every
# episode in runs/) already spent, and is the worst case preview works out at N for the episode's own per-replay
# cap, rounded up to $50, so a full diagnose is never stopped by its default cap.
CAP_ROUNDING_USD = 50
QUEUE_EPISODES = 20  # the batch total covers this many episodes at the floor's worst case


def _worst_case_cap(replay_cap_usd: float, oracles) -> float:
    worst = diagnose_cost(replay_cap_usd, oracles, n=RUNS_PER_VARIANT, spent_usd=0.0, cap_usd=float("inf"),
                          drafts=sum(c in DRAFTED_CAUSES for c in oracles), effort_step=True,
                          family_step=True).max_usd
    return float(-(-worst // CAP_ROUNDING_USD) * CAP_ROUNDING_USD)


def episode_cap_usd(replay_cap_usd: float) -> float:
    """One episode's baseline and every oracle at its per-replay cap ($3,900 at the $10 floor, N = 10)."""
    return _worst_case_cap(replay_cap_usd, ORACLE_ORDER)


def fault_cap_usd(replay_cap_usd: float) -> float:
    """A planted fault never has an environment fix to run ($3,000 at the $10 floor, N = 10)."""
    return _worst_case_cap(replay_cap_usd, [c for c in ORACLE_ORDER if c is not Cause.ENVIRONMENT])


# every episode `diagnose` with no id takes on, sized at the floor ($78,000); each episode keeps its own cap inside it
TOTAL_CAP_USD = QUEUE_EPISODES * episode_cap_usd(REPLAY_BUDGET_FLOOR_USD)

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.runs, args.projects = default_runs(), default_projects()
    try:
        if getattr(args, "needs_store", False):
            require_store(args.runs)
        if getattr(args, "episode_id", None):
            typed = args.episode_id
            args.episode_id = resolve_episode_id(args.runs, typed)
            args.ref = episode_ref(args.runs, typed, args.episode_id)  # how this output names it in commands
        return args.handler(args)
    except NotReady as missing:
        err(f"culprit: {missing}")
        return EXIT_NOT_READY
    except BudgetExhausted as spent:
        err(f"culprit: {spent}")
        return EXIT_BUDGET
    except KeyboardInterrupt:
        # every replay is saved as soon as it is judged and reused on the next run, so nothing paid for is lost
        err("\nculprit: stopped. finished runs are kept; rerun the same command to resume")
        return EXIT_INTERRUPTED
    except (UnknownEpisode, EpisodeNotFound, FileNotFoundError, ClaudeCallFailed,
            SetupScriptFailed, UnknownRun, OriginalModified, NotReplayable) as refusal:
        err(f"culprit: {refusal}")
        return EXIT_REFUSED


def say(lines: Sequence[str]) -> None:
    """Lines for the person, on stdout in reading order."""
    for line in lines:
        print(line)


def err(line: str) -> None:
    """A progress line, warning or refusal on stderr; stdout is flushed first, so where both streams reach one
    terminal nothing shows out of order."""
    sys.stdout.flush()
    print(line, file=sys.stderr, flush=True)


def default_runs() -> Path:
    """The store: $CULPRIT_RUNS when set, otherwise runs/ in the current folder."""
    return Path(os.environ.get(RUNS_ENV) or "runs")


def default_projects() -> Path:
    """The session logs: $CULPRIT_PROJECTS when set, otherwise ~/.claude/projects."""
    return Path(os.environ.get(PROJECTS_ENV) or logs.DEFAULT_PROJECTS)


def require_store(runs_dir: Path) -> None:
    """Read-only commands call this so a wrong folder is reported instead of an empty store being created."""
    if not runs_dir.is_dir():
        raise NotReady(f"no culprit store at {runs_dir.resolve()} — run from the folder holding runs/ "
                       f"or set {RUNS_ENV}")


COMMAND_GROUPS = {
    "workflow": ("triage", "env-fix", "diagnose", "report", "status"),
    "human review": ("confirm",),
    "trust": ("review", "selftest"),
}
EXAMPLE_RUN = """example run:
  culprit triage --top 20                  rank likely failures; rows are numbered 1, 2, ...
  culprit confirm                          walk the queue: say what went wrong, approve what success means
  culprit diagnose                         replay every confirmed episode, find the layer to change
  culprit report                           rank the changes across episodes
  culprit status                           where every episode stands, and what to run next
one episode at a time:
  culprit confirm 1 wrong-result          record it, draft what success means, ask to approve it
  culprit diagnose 1                      restore, replay, find the layer to change
without a terminal:
  culprit confirm 1                       what happened and a suggested symptom; records nothing
  culprit confirm 1 wrong-result          record it and draft what success means
  culprit status 1                        read the draft (edit runs/<episode>/criterion.json if needed)
  culprit confirm 1 approve               approve it
  culprit diagnose 1                      restore, replay, find the layer to change
"""
EXAMPLES = {
    "triage": "  culprit triage --top 40\n  CULPRIT_PROJECTS=~/other/projects CULPRIT_RUNS=~/work/runs culprit triage\n",
    "confirm": ("  culprit confirm                         each queued episode in turn: symptom, criterion, restore\n"
                "  culprit confirm 1 wrong-result         record it, draft what success means, ask to approve it\n"
                "  culprit confirm 1                      what happened and a suggested symptom (Enter accepts it);\n"
                "                                         once a symptom is recorded, the draft again to approve\n"
                "  culprit confirm 1 approve              approve the draft without a terminal\n"
                "  culprit confirm 1a2b3c4d:5e6f7a8b no-failure\n"),
    "status": "  culprit status\n  culprit status 1\n  culprit status --json   one JSON document, for scripts\n",
    "env-fix": "  culprit env-fix 1 fix-env.sh         run before Claude starts in the environment oracle\n",
    "diagnose": "  culprit diagnose 1\n  culprit diagnose       every confirmed episode in the queue, then the report\n",
    "review": "  culprit review        answer p(ass), f(ail), s(kip) or q(uit) for each judged replay\n",
    "selftest": "  culprit selftest #4\n",
    "report": "  culprit report\n  CULPRIT_RUNS=~/work/runs culprit report\n",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", usage="culprit [-h] <command> ...", epilog=EXAMPLE_RUN,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # the commands are listed by group in the description, so argparse's flat list is hidden
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>", help=argparse.SUPPRESS)
    summaries: dict[str, str] = {}

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        summaries[name] = summary
        return commands.add_parser(name, description=summary, epilog="examples:\n" + EXAMPLES[name],
                                   formatter_class=argparse.RawDescriptionHelpFormatter)

    triage = add("triage", "rank episodes most likely to have failed")
    triage.add_argument("--top", type=int, default=20)
    triage.set_defaults(handler=triage_command)

    confirm = add("confirm", "say what went wrong and approve what success means (no id: walk the queue)")
    confirm.add_argument("episode_id", nargs="?")
    confirm.add_argument("symptom", nargs="?", choices=[*(s.value for s in Symptom), APPROVE], metavar="symptom",
                         help=f"what went wrong: {', '.join(s.value for s in Symptom)}; or {APPROVE}, which approves "
                              f"the drafted success criterion")
    confirm.set_defaults(handler=confirm_command, needs_store=True)

    status = add("status", "where each episode stands and the command that moves it on")
    status.add_argument("episode_id", nargs="?")
    status.add_argument("--json", action="store_true", help="print one JSON document on stdout instead of text")
    status.set_defaults(handler=status_command, needs_store=True)

    env_fix = add("env-fix", "supply a script that fixes the environment for the environment oracle")
    env_fix.add_argument("episode_id")
    env_fix.add_argument("script", type=Path)
    env_fix.set_defaults(handler=env_fix_command)

    diagnose_parser = add("diagnose", "restore, replay and find what to change (no id: the whole queue, then report)")
    diagnose_parser.add_argument("episode_id", nargs="?")
    diagnose_parser.set_defaults(handler=diagnose_command)

    review = add("review", "label judged replays pass or fail, one after another, to calibrate the judge")
    review.set_defaults(handler=review_command, needs_store=True)

    selftest = add("selftest", "plant known faults into a control episode and score culprit")
    selftest.add_argument("episode_id")
    selftest.set_defaults(handler=selftest_command)

    report = add("report", "write runs/report.md from every stored verdict")
    report.set_defaults(handler=report_command, needs_store=True)

    width = max(map(len, summaries)) + 2
    parser.description = "Find what to change when a Claude Code episode fails.\n" + "".join(
        f"\n{group}:\n" + "".join(f"  {name:<{width}}{summaries[name]}\n" for name in names)
        for group, names in COMMAND_GROUPS.items())
    return parser


def emit(data: dict) -> None:
    """The whole of stdout under `status --json`: one document, so scripts can parse it."""
    print(json.dumps(data, indent=2, default=str))


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def require_terminal(why: str, otherwise: str = "") -> None:
    """Commands that ask the person questions refuse to run without someone at the keyboard."""
    if not stdin_is_tty():
        raise NotReady(f"{why}, so it needs a terminal" + (f" — otherwise {otherwise}" if otherwise else ""))


def show_plan(plan: CostPlan) -> None:
    """The most a command can run and spend, on stderr before it starts: information, not a question."""
    err("")  # set apart from the stdout block above it when both reach one terminal
    for line in plan.lines():
        err(line)


def triage_command(args: argparse.Namespace) -> int:
    summaries = [summary for session_file in session_files(args.projects) for summary in summarize_session(session_file)]
    ranked = rank_candidates(summaries, args.top)
    blockers = {c.summary.episode_id: _blockers_for(args.projects, c.summary.episode_id) for c in ranked}
    # A row a replay cannot use goes after every row it can; the sort is stable, so score order holds within each.
    candidates = sorted(ranked, key=lambda c: bool(blockers[c.summary.episode_id]))
    path = save_candidates(args.runs, candidates)
    queue = queued_ids(args.runs)
    columns = ("ROW", "SCORE", "EPISODE", "STAGE", "EVIDENCE", "PROMPT")
    rows = [(str(queue.index(c.summary.episode_id) + 1), str(c.score), display_id(args.runs, c.summary.episode_id),
             stage_mark(episode_status(args.runs, c.summary.episode_id)), _evidence(c.summary.signals),
             c.summary.prompt_excerpt) for c in candidates]
    widths = [max([len(column), *(len(row[i]) for row in rows)]) for i, column in enumerate(columns)]
    lines = [table_row(columns, widths)] if rows else []
    for row, candidate in zip(rows, candidates):
        lines.append(table_row(row, widths))
        indent = " " * (widths[0] + widths[1] + 4)  # sub-lines start under the episode
        s = candidate.summary
        if s.signals.api_errors:
            lines.append(f"{indent}{s.signals.api_errors} API errors — likely not reproducible")
        if blockers[s.episode_id]:
            lines.append(f"{indent}not replayable: {'; '.join(blockers[s.episode_id])}")
    say(lines + ["", triage_summary(len(summaries), len(candidates), len(queue)),
                 f"Saved the queue in {shown_runs(args.runs) / path.name}"]
        + next_section([Step("Say what went wrong and approve what success means, row by row", "culprit confirm"),
                        Step("Or one row at a time", "culprit confirm <row> <symptom>")]))
    # triage calls no model: the oracle is named as last resolved, or by its alias
    if warning := unknown_model_warning((s.model for s in summaries), known_oracle_id(args.runs) or oracle_model()):
        err(f"culprit: warning: {warning}")
    return 0


def table_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Cells padded to their column's width (numbers right-aligned under a heading of their own), the last unpadded."""
    padded = [cell.rjust(width) if i < 2 else cell.ljust(width)
              for i, (cell, width) in enumerate(zip(cells[:-1], widths))]
    return "  ".join([*padded, cells[-1]]).rstrip()


def triage_summary(scanned: int, shown: int, queued: int) -> str:
    if not shown:
        return f"Scanned {scanned} episodes; none looks like a failure" + (f" ({queued} queued earlier)" if queued else "")
    if shown == queued:
        return f"Scanned {scanned} episodes; the {shown} shown {'is' if shown == 1 else 'are'} the whole queue"
    rows = "is row 1" if shown == 1 else f"are rows 1–{shown}"
    more = queued - shown
    return (f"Scanned {scanned} episodes; the top {shown} {rows} of the {queued} queued; {more} more "
            f"{'is' if more == 1 else 'are'} queued but not shown — see culprit status")


def stage_mark(status: EpisodeStatus) -> str:
    """How far an episode has got, short enough for a triage column; blank until it is confirmed."""
    if status.stage == "diagnosed" and status.verdict:
        return f"diagnosed: {status.verdict.cause}"
    return {"not queued": "", "queued": "", "criterion drafted": "draft",
            "criterion approved": "approved"}.get(status.stage, status.stage)


def _blockers_for(projects: Path, episode_id: str) -> list[str]:
    episode = find_episode(projects, episode_id)
    return replay_blockers(episode, load_snapshot(episode))


def _evidence(signals: FailureSignals) -> str:
    parts = []
    if signals.interrupted:
        parts.append("interrupted")
    if signals.tool_errors:
        parts.append(f"{signals.tool_errors} tool errors")
    if signals.api_errors:
        parts.append(f"{signals.api_errors} API errors")
    if signals.compactions:
        parts.append(f"{signals.compactions} compactions")
    parts.append(f"{signals.duration_s:.0f}s")
    return ", ".join(parts)


APPROVE = "approve"  # in the symptom's place: approve the drafted criterion, without a terminal
APPROVE_OPTIONS = ("approve", "edit first", "skip")


def symptoms_needed(ref: str) -> str:
    return (f"say what went wrong — `culprit confirm {ref} <symptom>`, where <symptom> is one of "
            f"{', '.join(s.value for s in Symptom)}")


def suggestion_for(runs_dir: Path, episode_id: str, recap: WhatHappened | None) -> Suggestion | None:
    """The stored suggestion, else one model call shown only the recap's facts, charged to the episode at its cap
    and stored; None when there are no facts or the call fails (the question is then asked with no default)."""
    if (stored := load_suggestion(runs_dir, episode_id)) is not None or recap is None:
        return stored
    err("\nsuggesting a symptom from what happened (one model call)…")
    record_call_spend(episode_dir(runs_dir, episode_id), CALL_CAP_USD)
    try:
        suggestion = suggest_symptom(recap.as_text(), ask=ask_claude)
    except ClaudeCallFailed as failure:
        err(f"culprit: no symptom suggested: {failure}")
        return None
    save_suggestion(runs_dir, episode_id, suggestion)
    return suggestion


def ask_symptom(runs_dir: Path, episode_id: str, recap: WhatHappened | None, extra: Sequence[str]) -> str:
    """Suggest a symptom (below the recap already shown), list the five with their meanings and ask; Enter takes
    the suggestion. Returns a symptom's value or one of extra ("skip", "quit")."""
    suggestion = suggestion_for(runs_dir, episode_id, recap)
    say((suggestion_lines(suggestion) if suggestion else []) + symptoms_block(numbered=True))
    return choose("\nWhat went wrong?", [*(s.value for s in Symptom), *extra],
                  default=suggestion.symptom.value if suggestion else None)


def stored_suggestion_lines(runs_dir: Path, episode_id: str) -> list[str]:
    """A suggestion made earlier, shown again without a call."""
    suggestion = load_suggestion(runs_dir, episode_id)
    return suggestion_lines(suggestion) if suggestion else []


def confirm_command(args: argparse.Namespace) -> int:
    if args.episode_id is None:
        return confirm_queue(args)
    if args.symptom == APPROVE:
        return confirm_approve(args)
    return confirm_one(args)


def confirm_one(args: argparse.Namespace) -> int:
    """Record the symptom (asked in a terminal when none is given or recorded), draft the criterion when there is
    none, show it, and in a terminal ask whether to approve it. Confirming a symptom never approves anything."""
    before = read_symptom(args.runs, args.episode_id)
    recap = recap_for(args.projects, args.episode_id)
    say(episode_header(args.runs, args.episode_id, args.ref) + (["", *what_happened_block(recap)] if recap else []))
    if args.symptom is None and before is None and not stdin_is_tty():
        return show_suggestion(args, recap)
    if args.symptom is None and before is None:
        answer = ask_symptom(args.runs, args.episode_id, recap, ["skip"])
        if answer == "skip":
            say(next_section([Step("Say what went wrong", f"culprit confirm {args.ref} <symptom>")]))
            return 0
    else:
        say(stored_suggestion_lines(args.runs, args.episode_id))
        answer = args.symptom or before.value
    say(["", symptom_line(args.runs, args.episode_id, before, Symptom(answer))])
    kept = False
    if args.symptom:
        lines, kept = redraft_for(args.runs, args.projects, args.episode_id, Symptom(args.symptom))
        say(lines)
    answer = settle_criterion(args.runs, args.projects, args.episode_id, ask=stdin_is_tty(),
                              options=APPROVE_OPTIONS)
    say(next_section(criterion_next(args.runs, args.episode_id, args.ref, answer,
                                    misfit=Symptom(args.symptom) if kept else None)))
    return 0


def show_suggestion(args: argparse.Namespace, recap: WhatHappened | None) -> int:
    """Without a terminal and with no symptom: suggest one and list the five, then name the commands that record
    it. Records nothing — the person chooses by running one."""
    suggestion = suggestion_for(args.runs, args.episode_id, recap)
    steps = [Step("Say what went wrong", f"culprit confirm {args.ref} <symptom>")]
    if suggestion:
        steps = [Step("Record the suggested symptom", f"culprit confirm {args.ref} {suggestion.symptom.value}"),
                 Step("Or say another one", f"culprit confirm {args.ref} <symptom>")]
    say((suggestion_lines(suggestion) if suggestion else []) + symptoms_block() + next_section(steps))
    return 0


def symptom_line(runs_dir: Path, episode_id: str, before: Symptom | None, symptom: Symptom) -> str:
    """Record the symptom unless it is already the one recorded; say which it was."""
    if before == symptom:
        return f"{DONE} What went wrong: {symptom.value} (recorded earlier)"
    record_symptom(runs_dir, episode_id, symptom)
    if before is None:
        return f"{DONE} Recorded what went wrong: {symptom.value}"
    return f"{DONE} Changed what went wrong: {before.value} → {symptom.value}"


def redraft_for(runs_dir: Path, projects_dir: Path, episode_id: str, symptom: Symptom) -> tuple[list[str], bool]:
    """Redraft an unapproved draft written for another symptom (or before drafts were tagged with one) when the
    person has not edited it; keep an edited one and say what it was written for. An approved criterion is never
    redrafted. Returns the lines saying which happened (none when the draft already fits the symptom) and whether
    an edited draft written for another symptom was kept."""
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is None or criterion.symptom == symptom.value:
        return [], False
    if criterion.approved:
        if criterion.symptom is None:
            return [], False
        return ["", *wrap(f"The approved criterion was written for {criterion.symptom}, not {symptom.value}; "
                          f"approved criteria are never redrafted")], False
    if criterion_edited(criterion):
        path = short_path(runs_dir, episode_id, CRITERION_FILE)
        return ["", *wrap(f"Kept your edited draft; it was written for {criterion.symptom} — edit {path} to match "
                          f"{symptom.value}")], True
    err(f"\nredrafting what success means for {symptom.value} (one model call)…")
    draft_criterion_for(runs_dir, projects_dir, episode_id)
    return ["", f"{DONE} Redrafted for {symptom.value}"], False


def settle_criterion(runs_dir: Path, projects_dir: Path, episode_id: str, *, ask: bool,
                     options: Sequence[str]) -> str:
    """Draft the criterion when there is none (a failed draft raises), show a draft and, when asking, let the
    person approve it. Returns "approved", or the answer ("edit first", "skip", "quit"), or "shown" when not asked."""
    criterion = load_criterion(runs_dir, episode_id)
    if criterion is not None and criterion.approved:
        say(["", f"{DONE} Success criterion already approved"])
        return "approved"
    if criterion is None:
        err("\ndrafting what success means (one model call)…")
        criterion = draft_criterion_for(runs_dir, projects_dir, episode_id)
    say(["", *criterion_block(criterion)])
    if not ask:
        return "shown"
    answer = choose("\nApprove it?", options)  # the blank line goes with the question, which input() prints
    if answer == "approve":
        approve_criterion(runs_dir, episode_id)
        say(["", f"{DONE} Approved the success criterion"])
        return "approved"
    return answer


def criterion_next(runs_dir: Path, episode_id: str, ref: str, answer: str,
                   misfit: Symptom | None = None) -> list[Step]:
    """The next steps after confirm: diagnose once approved; otherwise edit or read the draft, then approve it.
    misfit names the symptom a kept edited draft was not written for: the edit is then to fit it to that."""
    path = short_path(runs_dir, episode_id, CRITERION_FILE)
    if answer == "approved":
        return [Step("Diagnose it", f"culprit diagnose {ref}")]
    if misfit is not None:
        hint = " (for wasteful, add limits)" if misfit == Symptom.WASTEFUL else ""
        return [Step(f"Edit {path} so it fits {misfit.value}{hint}"),
                Step("Then read it again and approve it", f"culprit confirm {ref}")]
    if answer == "edit first":
        return [Step(f"Edit {path}"), Step("Then read it again and approve it", f"culprit confirm {ref}")]
    criterion = load_criterion(runs_dir, episode_id)
    read = (f"Read the draft and check its limits. To change them, edit {path}" if criterion and criterion.limits
            else f"Read the draft. To change it, edit {path}")
    return [Step(read), Step("Approve it", f"culprit confirm {ref} {APPROVE}")]


def confirm_approve(args: argparse.Namespace) -> int:
    """`confirm <episode> approve`: approve the drafted criterion — the one way to approve without a terminal."""
    if read_symptom(args.runs, args.episode_id) is None:
        raise NotReady(symptoms_needed(args.ref))
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None:
        raise NotReady(f"no draft to approve for episode {args.ref} — `culprit confirm {args.ref}` drafts one")
    lines = episode_header(args.runs, args.episode_id, args.ref)
    if criterion.approved:
        lines += ["", f"{DONE} Success criterion already approved"]
    else:
        criterion = approve_criterion(args.runs, args.episode_id)
        lines += ["", f"{DONE} Approved the success criterion", "", *criterion_block(criterion)]
    say(lines + next_section(criterion_next(args.runs, args.episode_id, args.ref, "approved")))
    return 0


def choose(prompt: str, options: Sequence[str], read: Callable[[str], str] | None = None,
           default: str | None = None) -> str:
    """Ask until the answer picks one of options — its number, its first letter when no other option shares
    it, or the whole word — and return that word; Enter alone picks default when there is one, which the
    question names. End of input picks "quit" when it is an option."""
    question = (f"{prompt} " + " · ".join(f"{i} {word}" for i, word in enumerate(options, 1))
                + (f" — Enter for {default}" if default else "") + ": ")
    letters = [word[0] for word in options]
    picks = {**{str(i): word for i, word in enumerate(options, 1)}, **{word: word for word in options},
             **{word[0]: word for word in options if letters.count(word[0]) == 1}}
    while True:
        try:
            answer = (read or input)(question).strip().lower()
        except EOFError:
            if "quit" in options:
                return "quit"
            raise
        if answer in picks:
            return picks[answer]
        if not answer and default:
            return default


def confirm_queue(args: argparse.Namespace) -> int:
    """Walk the triage queue in order: for each episode not yet past every step, ask the symptom, draft and
    approve the criterion, and restore the starting files when a replay can use the episode."""
    require_terminal("confirm with no episode asks about each queued episode",
                     otherwise="run `culprit confirm <episode-id> <symptom>`")
    rows = require_queue(args.runs)
    shown_any = stopped = False
    for position, row in enumerate(rows, 1):
        episode_id = row["summary"]["episode_id"]
        approved, restored = _confirm_progress(args.runs, episode_id)
        if approved and restored:
            continue
        blockers = _walk_blockers(args, episode_id)
        if approved and blockers:  # restoring is the only step left, and it cannot be done
            continue
        shown_any = True
        if confirm_walk_step(args, position, row, blockers) == "quit":
            stopped = True
            break
    if not shown_any:
        say([f"{DONE} Nothing left to confirm: every queued episode is confirmed, approved and restored"])
    steps = [Step("Carry on where you stopped", "culprit confirm")] if stopped else []
    say(next_section(steps + [Step("Diagnose every confirmed episode", "culprit diagnose")]))
    return 0


def _walk_blockers(args: argparse.Namespace, episode_id: str) -> list[str]:
    try:
        return _blockers_for(args.projects, episode_id)
    except EpisodeNotFound as missing:
        return [str(missing)]


def stored_signals(stored: dict) -> FailureSignals:
    """Signals as candidates.json holds them; keys a past version wrote and this one dropped are ignored."""
    return FailureSignals(**{f.name: stored[f.name] for f in fields(FailureSignals) if f.name in stored})


def recap_for(projects: Path, episode_id: str) -> WhatHappened | None:
    """What happened in the episode, with the human's next prompt (so the person can judge whether it corrected
    Claude); None when the session log is gone."""
    try:
        episode = find_episode(projects, episode_id)
        return what_happened(episode, next(iter(follow_up_prompts(projects, episode_id, count=1)), None))
    except EpisodeNotFound:
        return None


def recap_lines(projects: Path, episode_id: str) -> list[str]:
    """The What happened block, set apart by a blank line; nothing when the session log is gone."""
    recap = recap_for(projects, episode_id)
    return ["", *what_happened_block(recap)] if recap else []


def _confirm_progress(runs_dir: Path, episode_id: str) -> tuple[bool, bool]:
    """(symptom recorded and criterion approved, starting files restored or episode already diagnosed)."""
    criterion = load_criterion(runs_dir, episode_id)
    folder = episode_dir(runs_dir, episode_id)
    approved = read_symptom(runs_dir, episode_id) is not None and criterion is not None and criterion.approved
    return approved, pristine_dir(runs_dir, episode_id).is_dir() or load_verdict(folder) is not None


def confirm_walk_step(args: argparse.Namespace, position: int, row: dict, blockers: list[str]) -> str:
    """One episode of the walk; returns "quit" when the person wants to stop."""
    summary = row["summary"]
    episode_id = summary["episode_id"]
    signals = summary.get("signals")
    lines = ["", *episode_header(args.runs, episode_id, str(position))]
    recap = recap_for(args.projects, episode_id)
    if not recap and signals:  # the session log is gone: what triage saw of it is all there is
        lines.append(f"  {_evidence(stored_signals(signals))}")
    lines += [line for blocker in blockers for line in wrap(f"Not replayable: {blocker}", "  ")]
    say(lines + (["", *what_happened_block(recap)] if recap else []))
    before = read_symptom(args.runs, episode_id)
    if before is None:
        answer = ask_symptom(args.runs, episode_id, recap, ["skip", "quit"])
        if answer in ("skip", "quit"):
            return answer
    else:
        say(stored_suggestion_lines(args.runs, episode_id))
    say(["", symptom_line(args.runs, episode_id, before, before or Symptom(answer))])
    try:
        answer = settle_criterion(args.runs, args.projects, episode_id, ask=True,
                                  options=(*APPROVE_OPTIONS, "quit"))
    except (EpisodeNotFound, ClaudeCallFailed) as failure:
        say(wrap(f"Criterion not drafted: {failure}", ""))
        return "skip"
    if answer == "edit first":
        say(["", *wrap(f"Edit {short_path(args.runs, episode_id, CRITERION_FILE)}, then run `culprit confirm` again")])
    if answer != "approved":
        return "quit" if answer == "quit" else "skip"
    if not blockers and not pristine_dir(args.runs, episode_id).is_dir():
        say(["", *restored_lines(restore_episode(args.runs, args.projects, episode_id))])
    return "done"


def draft_criterion_for(runs_dir: Path, projects_dir: Path, episode_id: str) -> Criterion:
    """Draft what success means from the episode, the human's follow-ups and the recorded symptom (one model
    call) and save it."""
    episode = find_episode(projects_dir, episode_id)
    criterion = draft_criterion(episode, follow_up_prompts(projects_dir, episode_id),
                                read_symptom(runs_dir, episode_id), ask=ask_claude)
    save_criterion(runs_dir, episode_id, criterion)
    return criterion


def restored_lines(details: list[str]) -> list[str]:
    """What restore_episode found, under one done line."""
    return [f"{DONE} Restored the starting files", *(line for detail in details for line in wrap(detail, "    "))]


def status_command(args: argparse.Namespace) -> int:
    if args.episode_id:
        status = episode_status(args.runs, args.episode_id)
        if args.json:
            emit(status_json(status))
            return 0
        say(status_lines(args.runs, status, args.ref, recap_lines(args.projects, args.episode_id)
                         + stored_suggestion_lines(args.runs, args.episode_id)))
        return 0
    statuses = queue_status(args.runs)
    if args.json:
        correct, total = selftest_accuracy(args.runs)
        emit({"episodes": [status_json(status) for status in statuses], "judge": agreement_json(agreement(args.runs)),
              "selftest": {"correct": correct, "total": total}})
        return 0
    if not statuses:
        say(["Nothing queued yet.", *next_section([Step("Rank likely failures", "culprit triage")])])
        return 0
    say(status_table(args.runs, statuses) + ["", *trust_block(trust_lines(args.runs))]
        + next_section(queue_steps(statuses)))
    return 0


def status_table(runs_dir: Path, statuses: list[EpisodeStatus]) -> list[str]:
    """One row per episode: its queue row, short id, stage and the command that moves it on; the prompt beneath."""
    queue = queued_ids(runs_dir)
    shown = [display_id(runs_dir, status.episode_id) for status in statuses]
    rows = [(str(queue.index(status.episode_id) + 1) if status.episode_id in queue else "", name, status.stage,
             row_command(status, name)) for name, status in zip(shown, statuses)]
    id_width = max(len("Episode"), *map(len, shown))
    stage_width = max(len("Stage"), *(len(status.stage) for status in statuses))
    lines = ["Episodes", f"  ROW  {'EPISODE':<{id_width}}  {'STAGE':<{stage_width}}  DO NEXT"]
    for (row, name, stage, command), status in zip(rows, statuses):
        lines.append(f"  {row:>3}  {name:<{id_width}}  {stage:<{stage_width}}  {command}")
        if excerpt := prompt_excerpt(runs_dir, status.episode_id):
            lines += wrap(f'"{excerpt}"', " " * 7)
    return lines


def status_json(status: EpisodeStatus) -> dict:
    """The status as JSON, each variant's outcome with its cost and tokens per run (void attempts included)."""
    data = asdict(status)
    for outcome in data["outcomes"]:
        outcome["usd_per_run"], outcome["tokens_per_run"] = usd_per_run(outcome), tokens_per_run(outcome)
    return data


def status_lines(runs_dir: Path, status: EpisodeStatus, ref: str, recap: Sequence[str] = ()) -> list[str]:
    lines = [*episode_header(runs_dir, status.episode_id, ref), *recap,
             *block("Progress", progress_lines(runs_dir, status))]
    if status.criterion:
        lines += ["", *criterion_block(status.criterion)]
    if status.restore:
        r = status.restore
        lines += block("Starting files", wrap(
            f"The snapshot agrees with the episode's own record for {r.get('snapshot_agreed', 0)} of "
            f"{r.get('checked', 0)} files; {r.get('unknown', 0)} unknown, {r.get('missing_backups', 0)} missing backups",
            "  "))
    if status.outcomes:
        lines += ["", "Replays"]
        for outcome in status.outcomes:
            void = f", {outcome.void} void" if outcome.void else ""
            if outcome.limits:
                void += f", {sum(outcome.limits.values())} stopped at a limit ({limit_tally(outcome.limits)})"
            if outcome.network:
                void += (f", network {outcome.network.get('served', 0)} served, "
                         f"{outcome.network.get('refused', 0)} refused as unrecorded")
            lines.append(f"  {outcome.variant}: {outcome.passes} of {outcome.runs} passed{void}, ${outcome.cost_usd:.2f}")
        lines.append(f"  Spent so far: ${status.spent_usd:.2f}")
    if status.verdict:
        lines += ["", *verdict_lines(status.verdict)]
    return lines + next_section(episode_steps(runs_dir, status, ref))


class NotReplayable(Exception):
    """An episode a replay cannot use; the message names every reason."""


def restore_episode(runs_dir: Path, projects_dir: Path, episode_id: str) -> list[str]:
    """Rebuild the episode's starting files in runs/<episode>/pristine/ and store the trust figures (restore.json);
    returns what to tell the person. Refuses with NotReplayable when a replay cannot use the episode."""
    episode = find_episode(projects_dir, episode_id)
    tracked = load_snapshot(episode)
    blockers = replay_blockers(episode, tracked)
    if blockers:
        raise NotReplayable(f"{display_id(runs_dir, episode_id)} cannot be replayed: " + "; ".join(blockers))
    destination = pristine_dir(runs_dir, episode_id)
    report = restore(episode, tracked, destination, file_history_root=replay.DEFAULT_FILE_HISTORY,
                     session_root=session_root(episode.session_file))
    root = session_root(episode.session_file) or Path(episode.cwd)
    covered = {str(Path(path) if Path(path).is_absolute() else root / path) for path in tracked}
    covered |= set(episode_originals(episode))
    from_git = restore_from_git(episode, destination, covered)
    checked, mismatches = check_restore(episode, destination)  # the snapshot, before the episode's record overrides it
    written, removed = apply_episode_originals(episode, destination)
    lines = [f"{report.restored} restored from snapshots, {len(report.unknown)} unknown (snapshot kept no backup), "
             f"{len(report.missing_backups)} missing backups, {len(report.outside_cwd)} outside the working directory",
             f"snapshot agrees with the episode's own record for {checked - len(mismatches)} of {checked} files"]
    lines += [f"mismatch: {mismatch.path} ({mismatch.reason})" for mismatch in mismatches]
    lines.append(f"{written} file(s) set from the episode's own record, {removed} removed (created during the episode)")
    if from_git:
        lines.append(f"{from_git.restored} other file(s) set from git commit {from_git.commit[:10]} (the last before "
                     f"the episode), {from_git.removed} removed (added after it)")
    trust = {"git_commit": from_git.commit if from_git else None,
             "set_from_git": from_git.restored if from_git else 0,
             "removed_by_git": from_git.removed if from_git else 0, "snapshot_agreed": checked - len(mismatches),
             "checked": checked, "restored": report.restored,
             "unknown": len(report.unknown), "missing_backups": len(report.missing_backups),
             "outside_cwd": len(report.outside_cwd), "set_from_episode": written, "removed": removed}
    write_json(episode_dir(runs_dir, episode_id) / RESTORE_FILE, trust)
    hooks = project_hooks(Path(episode.cwd))
    lines.append("project hooks that will run in every replay: " + "; ".join(hooks) if hooks else "no project hooks")
    lines.append(f"in {short_path(runs_dir, episode_id, destination.name)}")
    return lines


def not_ready(runs_dir: Path, episode_id: str, ref: str, problem: str) -> NotReady:
    """A refusal that names what is missing, then the numbered steps that get the episode there."""
    steps = episode_steps(runs_dir, episode_status(runs_dir, episode_id), ref)
    return NotReady("\n".join([problem, *next_section(steps)]))


def approved_criterion(args: argparse.Namespace) -> Criterion:
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None or not criterion.approved:
        raise not_ready(args.runs, args.episode_id, args.ref,
                        f"episode {args.ref} has no approved success criterion yet")
    return criterion


def ready_to_replay(args: argparse.Namespace) -> tuple[Episode, Criterion]:
    """The episode and its approved criterion, under the episode's header; restores its starting files first
    when they are missing."""
    criterion = approved_criterion(args)
    say(episode_header(args.runs, args.episode_id, args.ref))
    episode = find_episode(args.projects, args.episode_id)
    if not pristine_dir(args.runs, args.episode_id).is_dir():
        say(["", *restored_lines(restore_episode(args.runs, args.projects, args.episode_id))])
    return episode, criterion


def run_baseline(args: argparse.Namespace, episode: Episode, criterion: Criterion) -> VariantOutcome:
    """Replay the unchanged episode N times under the episode cap and judge each run."""
    folder = episode_dir(args.runs, args.episode_id)
    spent = stored_spend(folder)
    base = baseline_settings(episode)
    cap = episode_cap_usd(base.max_budget_usd)
    show_plan(plan_cost([base.max_budget_usd], n=RUNS_PER_VARIANT, spent_usd=spent, cap_usd=cap))
    if retire_if_criterion_changed(folder, criterion):
        err("the criterion changed since the last runs: they were moved to superseded/ and will not be reused")
    budget = EpisodeBudget(cap, spent_usd=spent)
    with ProgressPrinter() as progress:
        outcome = run_variant(BASELINE, base, n=RUNS_PER_VARIANT,
                              episode=episode, runs_dir=args.runs, criterion=criterion, budget=budget,
                              progress=progress)
    say(["", "Replays", f"  baseline: {outcome.passes} of {outcome.runs} passed, ${outcome.cost_usd:.2f} spent on replays"])
    return outcome


def env_fix_command(args: argparse.Namespace) -> int:
    destination = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.script, destination)
    say([*episode_header(args.runs, args.episode_id, args.ref), "",
         f"{DONE} Stored the environment fix in {short_path(args.runs, args.episode_id, ENV_FIX_FILE)}",
         *wrap("The environment oracle runs it in each replay's copy before Claude starts.", "  "),
         *next_section(episode_steps(args.runs, episode_status(args.runs, args.episode_id), args.ref))])
    return 0


def diagnose_command(args: argparse.Namespace) -> int:
    if args.episode_id is None:
        return diagnose_queue(args)
    symptom = read_symptom(args.runs, args.episode_id)
    if symptom is None:
        raise not_ready(args.runs, args.episode_id, args.ref, f"episode {args.ref} has no confirmed symptom yet")
    episode, criterion = ready_to_replay(args)
    folder = episode_dir(args.runs, args.episode_id)
    env_script = env_fix_script(folder)
    base = baseline_settings(episode)
    # what the oracle alias means today, before the preview counts the model oracle; a paid resolution is the episode's
    oracle = resolved_model(args.runs, oracle_model())
    if oracle.usd:
        record_call_spend(folder, oracle.usd)
    if warning := unknown_model_warning([episode.model], oracle.model or oracle_model()):
        err(f"culprit: warning: {warning}")
    cap = episode_cap_usd(base.max_budget_usd)
    budget = EpisodeBudget(cap, spent_usd=stored_spend(folder))
    show_plan(diagnose_cost_for(folder, criterion, base.max_budget_usd, model=base.model, effort=base.effort,
                                oracle_id=known_oracle_id(args.runs),
                                env_fix=env_script is not None, n=RUNS_PER_VARIANT, spent_usd=budget.spent_usd,
                                cap_usd=cap))
    with ProgressPrinter() as progress:
        verdict = diagnose(episode, criterion, symptom, base,
                           runs_dir=args.runs, follow_ups=follow_up_prompts(args.projects, args.episode_id),
                           env_script=env_script, n=RUNS_PER_VARIANT, budget=budget,
                           progress=progress)
    steps = episode_steps(args.runs, episode_status(args.runs, args.episode_id), args.ref)
    if Cause.ENVIRONMENT.value in verdict.untested and env_script is None:
        steps.append(Step("To test the environment too, store a script that fixes it",
                          f"culprit env-fix {args.ref} <script>"))
    say(["", *verdict_lines(verdict), "", *trust_block(trust_lines(args.runs)), *next_section(steps)])
    return 0


def planted_line(correct: int, total: int) -> str:
    return f"{correct} of {total} planted faults named correctly"


def trust_lines(runs_dir: Path) -> list[str]:
    result = agreement(runs_dir)
    kappa = "not computable yet" if result.kappa is None else f"{result.kappa:.2f}"
    state = "calibrated" if result.calibrated else f"uncalibrated (needs {MIN_LABELS} labels and kappa ≥ {MIN_KAPPA})"
    correct, total = selftest_accuracy(runs_dir)
    planted = f"selftest: {planted_line(correct, total)}" if total else "selftest: not run"
    return [f"judge: {result.agreed} of {result.labelled} labels agree, kappa {kappa} — {state}", planted]


def review_command(args: argparse.Namespace) -> int:
    """Show each judged replay nobody has labelled and record the person's pass/fail, until none are left."""
    require_terminal("review asks for your verdict on each replay")
    skipped: set[Path] = set()
    lines = []
    while (run_dir := next_unlabelled(args.runs, skip=skipped)) is not None:
        say(["", replay_heading(args.runs, run_id(args.runs, run_dir)),
             *(f"  {line}" for line in run_summary(run_dir).splitlines())])
        answer = choose("\nYour verdict?", ("pass", "fail", "skip", "quit"))
        if answer == "quit":
            break
        if answer == "skip":
            skipped.add(run_dir)
        else:
            record_label(args.runs, run_id(args.runs, run_dir), answer == "pass")
    else:
        lines = ["", f"{DONE} Every judged replay is labelled" + (f" ({len(skipped)} skipped)" if skipped else "")]
    calibrated = agreement(args.runs).calibrated
    step = (Step("See where every episode stands", "culprit status") if calibrated
            else Step("Label more replays as diagnoses add them", "culprit review"))
    say(lines + ["", *trust_block(trust_lines(args.runs))] + next_section([step]))
    return 0


def replay_heading(runs_dir: Path, run: str) -> str:
    """`Replay · s1:p1 · baseline run 0` for runs/<episode>/replays/<variant>/<n>; other runs by their path."""
    folder, found, rest = run.partition("/replays/")
    if not found or "__" not in folder:
        return f"Replay · {run}"
    variant, _, index = rest.rpartition("/")
    return f"Replay · {display_id(runs_dir, folder.replace('__', ':', 1))} · {variant} run {index}"


def agreement_json(result) -> dict:
    return {"labelled": result.labelled, "agreed": result.agreed, "kappa": result.kappa,
            "calibrated": result.calibrated, "min_labels": MIN_LABELS, "min_kappa": MIN_KAPPA}


def selftest_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    baseline = load_outcome(episode_dir(args.runs, args.episode_id), BASELINE)
    if baseline is None:
        baseline = run_baseline(args, episode, criterion)
    if not is_control(args.runs, args.episode_id, RUNS_PER_VARIANT):
        err("\n".join([f"culprit: episode {args.ref} is not a control — its baseline passed {baseline.passes} of "
                       f"{baseline.runs}, and a control must pass at least {control_min_passes(RUNS_PER_VARIANT)} "
                       f"of {RUNS_PER_VARIANT}",
                       *next_section([Step("Plant faults in an episode that usually passes",
                                           "culprit selftest <episode>")])]))
        return EXIT_REFUSED
    base = baseline_settings(episode)
    per_fault = fault_cap_usd(base.max_budget_usd)
    show_plan(selftest_cost(args.runs, args.episode_id, criterion, base, n=RUNS_PER_VARIANT,
                            budget_per_fault=per_fault))
    with ProgressPrinter() as progress:
        results = run_selftest(episode, criterion, base, runs_dir=args.runs,
                               follow_ups=follow_up_prompts(args.projects, args.episode_id), n=RUNS_PER_VARIANT,
                               budget_per_fault=per_fault, progress=progress)
    faults = [f"{r.fault}: expected {r.expected}, got {r.got} — {'named correctly' if r.correct else 'missed'}"
              for r in results]
    say(["", "Planted faults", *numbered(faults, "  "), "", "Score",
         f"  {planted_line(sum(r.correct for r in results), len(results))}",
         *next_section([Step("See how far to trust the verdicts", "culprit status")])])
    return 0


def write_report(runs_dir: Path) -> str:
    text = render_report(collect(runs_dir), trust_lines(runs_dir))
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / REPORT_FILE).write_text(text)
    return text


def report_command(args: argparse.Namespace) -> int:
    print(write_report(args.runs), end="")
    return 0


def diagnose_queue(args: argparse.Namespace) -> int:
    """Every confirmed episode in the queue: restore the ones not yet restored, diagnose them all, then report."""
    require_queue(args.runs)
    restoring = []
    for episode_id in queued_ids(args.runs):
        approved, restored = _confirm_progress(args.runs, episode_id)
        if restored or not approved:
            continue
        shown = display_id(args.runs, episode_id)
        try:
            restore_episode(args.runs, args.projects, episode_id)
            restoring.append(f"  {DONE} {shown}")
        except (NotReplayable, EpisodeNotFound) as refusal:
            restoring += wrap(f"{shown} not restored: {refusal}", "    ", first="    ")
    if restoring:
        say(["Restoring starting files", *restoring, ""])
    show_plan(batch_cost(args.runs, args.projects, n=RUNS_PER_VARIANT, per_episode_usd=episode_cap_usd,
                         total_usd=TOTAL_CAP_USD))
    with ProgressPrinter() as progress:
        summary = diagnose_all(args.runs, args.projects, n=RUNS_PER_VARIANT, per_episode_usd=episode_cap_usd,
                               total_usd=TOTAL_CAP_USD, progress=progress)
    write_report(args.runs)
    say(batch_lines(args.runs, summary) + ["", *trust_block(trust_lines(args.runs))] + next_section(
        [Step(f"Read the report, saved in {shown_runs(args.runs) / REPORT_FILE}", "culprit report")]
        + ([Step("Carry on from where the total ran out", "culprit diagnose")] if summary.stopped_at else [])))
    return 0


def batch_lines(runs_dir: Path, summary) -> list[str]:
    """What `diagnose` with no id did: each episode diagnosed with its numbers, skipped, or stopped by a cap."""
    def shown(episode_id: str) -> str:
        return display_id(runs_dir, episode_id)

    done = []
    for episode_id in summary.diagnosed:
        verdict = load_verdict(episode_dir(runs_dir, episode_id))
        done.append(f"{shown(episode_id)}  {(verdict and numbers_line(verdict)) or (verdict.cause if verdict else '')}")
    lines = ["Diagnosed", *(numbered(done, "  ") or ["  none"])]
    if summary.skipped:
        lines += block("Skipped", numbered((f"{shown(i)}  {reason}" for i, reason in summary.skipped.items()), "  "))
    capped = [f"{shown(i)}  hit its ${cap:.2f} cap; its finished runs are kept" for i, cap in summary.over_cap.items()]
    if summary.stopped_at:
        capped.append(f"{shown(summary.stopped_at)}  stopped here: the ${TOTAL_CAP_USD:.2f} total for this command "
                      f"is spent")
    if capped:
        lines += block("Stopped at a spend cap", numbered(capped, "  "))
    return lines + ["", f"Spent ${summary.spent_usd:.2f}"]
