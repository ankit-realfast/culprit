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
                              diagnose_cost_for, env_fix_script, limit_tally, limits_line, load_outcome, load_verdict,
                              numbers_line, plan_cost,
                              pristine_dir, retire_if_criterion_changed, run_variant, stored_spend)
from culprit.logs import (Episode, EpisodeNotFound, FailureSignals, excerpt, find_episode, follow_up_prompts,
                          load_snapshot, project_hooks, rank_candidates, replay_blockers, session_files, session_root,
                          summarize_session)
from culprit.model import (CRITERION_FILE, LIMIT_WORDS, ClaudeCallFailed, Criterion, approve_criterion, ask_claude, criterion_step,
                           draft_criterion, load_criterion, save_criterion)
from culprit.replay import (DEFAULT_REPLAY_BUDGET_USD, OriginalModified, SetupScriptFailed, apply_episode_originals,
                            baseline_settings, check_restore, episode_originals, restore, restore_from_git)
from culprit.report import (REPORT_FILE, STAGES, EpisodeStatus, collect, episode_status, next_step, queue_status,
                            render_report)
from culprit.store import (ORACLE_ORDER, RESTORE_FILE, Cause, NotReady, Symptom, UnknownEpisode, confirm_step,
                           display_id, episode_dir, queued_ids, read_symptom, record_symptom, require_queue,
                           resolve_episode_id, save_candidates, write_json)
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

    Use it as a context manager so a heartbeat never outlives the command.
    """

    def __init__(self, stream: TextIO | None = None, *, interval: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.monotonic, heartbeat=Heartbeat):
        self.stream = stream or sys.stderr
        self.interval, self.clock, self.heartbeat = interval, clock, heartbeat
        self._running = None

    def __enter__(self) -> ProgressPrinter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._running is not None:
            self._running.stop()
            self._running = None

    def __call__(self, event: ReplayEvent) -> None:
        self.close()  # a replay that raised never sent its finish
        if event.kind == "start":
            started = self.clock()
            name = in_scope(event, event.variant)
            self._running = self.heartbeat(
                lambda: self.print(f"… still running {name} #{event.index}, {elapsed(self.clock() - started)}"),
                self.interval)
            return
        result = "void, not counted" if event.result == "void" else event.result
        if event.limit and event.result != "void":
            result += f" (stopped at its {LIMIT_WORDS.get(event.limit, event.limit)})"
        reused = " — reused from an earlier run" if event.reused else ""
        self.print(f"{describe(event)}: {result}, ${event.cost_usd:.2f} (episode spent ${event.spent_usd:.2f})"
                   f"{reused}")

    def print(self, line: str) -> None:
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
# episode in runs/) already spent, and is the worst case preview works out at N, rounded up to $50, so a full
# diagnose is never stopped by its default cap.
CAP_ROUNDING_USD = 50
QUEUE_EPISODES = 20  # the batch total covers this many episodes at their worst case


def _worst_case_cap(oracles) -> float:
    worst = diagnose_cost(DEFAULT_REPLAY_BUDGET_USD, oracles, n=RUNS_PER_VARIANT, spent_usd=0.0, cap_usd=float("inf"),
                          drafts=sum(c in DRAFTED_CAUSES for c in oracles)).max_usd
    return float(-(-worst // CAP_ROUNDING_USD) * CAP_ROUNDING_USD)


EPISODE_CAP_USD = _worst_case_cap(ORACLE_ORDER)  # one episode's baseline and every oracle ($850 at N = 10)
# a planted fault never has an environment fix to run ($650 at N = 10)
FAULT_CAP_USD = _worst_case_cap([c for c in ORACLE_ORDER if c is not Cause.ENVIRONMENT])
TOTAL_CAP_USD = QUEUE_EPISODES * EPISODE_CAP_USD  # every episode `diagnose` with no id takes on ($17,000)

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.runs, args.projects = default_runs(), default_projects()
    try:
        if getattr(args, "needs_store", False):
            require_store(args.runs)
        if getattr(args, "episode_id", None):
            args.episode_id = resolve_episode_id(args.runs, args.episode_id)
        return args.handler(args)
    except NotReady as missing:
        print(f"culprit: {missing}", file=sys.stderr)
        return EXIT_NOT_READY
    except BudgetExhausted as spent:
        print(f"culprit: {spent}", file=sys.stderr)
        return EXIT_BUDGET
    except KeyboardInterrupt:
        # every replay is saved as soon as it is judged and reused on the next run, so nothing paid for is lost
        print("\nculprit: stopped. finished runs are kept; rerun the same command to resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (UnknownEpisode, EpisodeNotFound, FileNotFoundError, ClaudeCallFailed,
            SetupScriptFailed, UnknownRun, OriginalModified, NotReplayable) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return EXIT_REFUSED


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
one episode at a time (no terminal needed):
  culprit confirm 1 wrong-result          record it and draft what success means
  culprit status 1                        read the draft (edit runs/<episode>/criterion.json if needed)
  culprit confirm 1 wrong-result          approve it
  culprit diagnose 1                      restore, replay, find the layer to change
"""
EXAMPLES = {
    "triage": "  culprit triage --top 40\n  CULPRIT_PROJECTS=~/other/projects CULPRIT_RUNS=~/work/runs culprit triage\n",
    "confirm": ("  culprit confirm                         each queued episode in turn: symptom, criterion, restore\n"
                "  culprit confirm 1 wrong-result         record it and draft what success means\n"
                "  culprit confirm 1 wrong-result         again, once the draft reads right: approve it\n"
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
    confirm.add_argument("symptom", nargs="?", choices=[s.value for s in Symptom])
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
    for line in plan.lines():
        print(line, file=sys.stderr)


def triage_command(args: argparse.Namespace) -> int:
    summaries = [summary for session_file in session_files(args.projects) for summary in summarize_session(session_file)]
    ranked = rank_candidates(summaries, args.top)
    blockers = {c.summary.episode_id: _blockers_for(args.projects, c.summary.episode_id) for c in ranked}
    # A row a replay cannot use goes after every row it can; the sort is stable, so score order holds within each.
    candidates = sorted(ranked, key=lambda c: bool(blockers[c.summary.episode_id]))
    path = save_candidates(args.runs, candidates)
    queue = queued_ids(args.runs)
    shown = [display_id(args.runs, c.summary.episode_id) for c in candidates]
    id_width = max(map(len, shown), default=0)
    for name, candidate in zip(shown, candidates):
        s = candidate.summary
        row = queue.index(s.episode_id) + 1
        print(f"{row:>4} {candidate.score:>3}  {name:<{id_width}}  [{_evidence(s.signals)}]  {s.prompt_excerpt}")
        if s.signals.api_errors:
            print(f"            {s.signals.api_errors} API errors — likely not reproducible")
        if blockers[s.episode_id]:
            print(f"            not replayable: {'; '.join(blockers[s.episode_id])}")
    print(f"{len(summaries)} episodes scanned; top {len(candidates)} merged into {path} ({len(queue)} queued); "
          f"refer to an episode by its row number, short id or any unique prefix")
    if len(queue) > len(candidates):
        print(f"{len(queue) - len(candidates)} more queued, not in this top {len(candidates)} — see `culprit status`")
    return 0


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


def confirm_command(args: argparse.Namespace) -> int:
    if args.episode_id is None:
        return confirm_queue(args)
    if args.symptom is None:
        raise NotReady(f"say what went wrong — `culprit confirm {display_id(args.runs, args.episode_id)} <symptom>`, "
                       f"where <symptom> is one of {', '.join(s.value for s in Symptom)}")
    return confirm_one(args)


def confirm_one(args: argparse.Namespace) -> int:
    """Record the symptom, then move the criterion on: draft it when there is none, approve the draft when the
    same symptom is confirmed again (the person has seen it by then), otherwise show it again."""
    shown = display_id(args.runs, args.episode_id)
    before = read_symptom(args.runs, args.episode_id)
    path = record_symptom(args.runs, args.episode_id, Symptom(args.symptom))
    print(f"recorded {args.symptom} for {shown} in {path}")
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None:
        criterion = draft_criterion_for(args.runs, args.projects, args.episode_id)
        print(f"drafted {episode_dir(args.runs, args.episode_id) / CRITERION_FILE}:")
        show_draft(args.runs, args.episode_id, criterion, args.symptom)
    elif not criterion.approved and before is not None and before.value == args.symptom:
        approve_criterion(args.runs, args.episode_id)
        print(f"approved the criterion for {shown}")
    elif not criterion.approved:
        show_draft(args.runs, args.episode_id, criterion, args.symptom)
    print_next(args.runs, args.episode_id)
    return 0


def choose(prompt: str, options: Sequence[str], read: Callable[[str], str] | None = None) -> str:
    """Ask until the answer picks one of options — its number, its first letter when no other option shares
    it, or the whole word — and return that word. End of input picks "quit" when it is an option."""
    question = f"{prompt} " + " · ".join(f"{i} {word}" for i, word in enumerate(options, 1)) + ": "
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


def confirm_queue(args: argparse.Namespace) -> int:
    """Walk the triage queue in order: for each episode not yet past every step, ask the symptom, draft and
    approve the criterion, and restore the starting files when a replay can use the episode."""
    require_terminal("confirm with no episode asks about each queued episode",
                     otherwise="run `culprit confirm <episode-id> <symptom>`")
    rows = require_queue(args.runs)
    shown_any = False
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
            break
    else:
        if not shown_any:
            print("nothing left to confirm: every queued episode is confirmed, approved and restored")
    print("next: culprit diagnose", file=sys.stderr)
    return 0


def _walk_blockers(args: argparse.Namespace, episode_id: str) -> list[str]:
    try:
        return _blockers_for(args.projects, episode_id)
    except EpisodeNotFound as missing:
        return [str(missing)]


def stored_signals(stored: dict) -> FailureSignals:
    """Signals as candidates.json holds them; keys a past version wrote and this one dropped are ignored."""
    return FailureSignals(**{f.name: stored[f.name] for f in fields(FailureSignals) if f.name in stored})


def _next_prompt(projects: Path, episode_id: str) -> str | None:
    """The human's next prompt after the episode, so the person can judge whether it corrected Claude."""
    try:
        return next(iter(follow_up_prompts(projects, episode_id, count=1)), None)
    except EpisodeNotFound:
        return None


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
    evidence = f"  [{_evidence(stored_signals(signals))}]" if signals else ""
    print(f"\n{position} {display_id(args.runs, episode_id)}{evidence}  {summary.get('prompt_excerpt', '')}")
    if follow_up := _next_prompt(args.projects, episode_id):
        print(f"    you said next: {excerpt(follow_up)}")
    for blocker in blockers:
        print(f"    not replayable: {blocker}")
    symptom = read_symptom(args.runs, episode_id)
    if symptom is None:
        answer = choose("what went wrong?", [*(s.value for s in Symptom), "skip", "quit"])
        if answer in ("skip", "quit"):
            return answer
        symptom = Symptom(answer)
        record_symptom(args.runs, episode_id, symptom)
    else:
        print(f"symptom: {symptom.value}")
    criterion = load_criterion(args.runs, episode_id)
    if criterion is None:
        try:
            criterion = draft_criterion_for(args.runs, args.projects, episode_id)
        except (EpisodeNotFound, ClaudeCallFailed) as failure:
            print(f"criterion not drafted: {failure}")
            return "skip"
    if not criterion.approved:
        print_criterion(criterion)
        answer = choose("approve it?", ("approve", "edit later", "skip", "quit"))
        if answer == "edit later":
            print(f"edit {episode_dir(args.runs, episode_id) / CRITERION_FILE}, then run `culprit confirm` again")
        if answer != "approve":
            return "quit" if answer == "quit" else "skip"
        approve_criterion(args.runs, episode_id)
        print("approved")
    if not blockers and not pristine_dir(args.runs, episode_id).is_dir():
        for line in restore_episode(args.runs, args.projects, episode_id):
            print(line)
    return "done"


def draft_criterion_for(runs_dir: Path, projects_dir: Path, episode_id: str) -> Criterion:
    """Draft what success means from the episode and the human's follow-ups (one model call) and save it."""
    episode = find_episode(projects_dir, episode_id)
    criterion = draft_criterion(episode, follow_up_prompts(projects_dir, episode_id), ask=ask_claude)
    save_criterion(runs_dir, episode_id, criterion)
    return criterion


def criterion_lines(criterion: Criterion) -> list[str]:
    state = "approved" if criterion.approved else "draft, not approved"
    return ([f"criterion [{state}]: {criterion.success_means}"] + [f"  check: {check}" for check in criterion.checks]
            + [f"  must not: {rule}" for rule in criterion.must_not])


def print_criterion(criterion: Criterion) -> None:
    for line in criterion_lines(criterion):
        print(f"  {line}")


def show_draft(runs_dir: Path, episode_id: str, criterion: Criterion, symptom: str) -> None:
    print_criterion(criterion)
    shown = display_id(runs_dir, episode_id)
    print(f"approve: rerun `culprit confirm {shown} {symptom}` once it says what success means "
          f"(edit {episode_dir(runs_dir, episode_id) / CRITERION_FILE} first if it does not)")


def print_next(runs_dir: Path, episode_id: str) -> None:
    """Every state-changing command ends by naming the step that follows, as `culprit status` would."""
    print(f"next: {next_step(runs_dir, episode_id)}", file=sys.stderr)


def status_command(args: argparse.Namespace) -> int:
    if args.episode_id:
        status = episode_status(args.runs, args.episode_id)
        if args.json:
            emit(asdict(status))
            return 0
        for line in status_lines(status):
            print(line)
        return 0
    statuses = queue_status(args.runs)
    if args.json:
        correct, total = selftest_accuracy(args.runs)
        emit({"episodes": [asdict(status) for status in statuses], "judge": agreement_json(agreement(args.runs)),
              "selftest": {"correct": correct, "total": total}})
        return 0
    if not statuses:
        print("nothing queued — run `culprit triage`")
    shown = [display_id(args.runs, status.episode_id) for status in statuses]
    id_width = max(map(len, shown), default=0)
    stage_width = max(map(len, STAGES))
    for name, status in zip(shown, statuses):
        print(f"{name:<{id_width}}  {status.stage:<{stage_width}}  next: {status.next_step}")
    print()
    for line in trust_lines(args.runs):
        print(line)
    return 0


def status_lines(status: EpisodeStatus) -> list[str]:
    lines = [status.episode_id, f"stage: {status.stage}", f"symptom: {status.symptom or 'not confirmed'}"]
    lines += criterion_lines(status.criterion) if status.criterion else ["criterion: none"]
    if status.restore:
        r = status.restore
        lines.append(f"restore: snapshot agrees for {r.get('snapshot_agreed', 0)} of {r.get('checked', 0)} files, "
                     f"{r.get('unknown', 0)} unknown, {r.get('missing_backups', 0)} missing backups")
    for outcome in status.outcomes:
        void = f", {outcome.void} void" if outcome.void else ""
        if outcome.limits:
            void += f", {sum(outcome.limits.values())} stopped at a limit ({limit_tally(outcome.limits)})"
        lines.append(f"{outcome.variant}: {outcome.passes} of {outcome.runs} passed{void}, ${outcome.cost_usd:.2f}")
    lines.append(f"spent: ${status.spent_usd:.2f}")
    if status.verdict:
        lines.append(f"cause: {status.verdict.cause} — {status.verdict.evidence}")
    lines.append(f"next: {status.next_step}")
    return lines


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
             f"{len(report.missing_backups)} missing backups, {len(report.outside_cwd)} outside the working directory "
             f"→ {destination}",
             f"snapshot agrees with the episode's own record for {checked - len(mismatches)} of {checked} files"]
    lines += [f"  mismatch: {mismatch.path} ({mismatch.reason})" for mismatch in mismatches]
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
    return lines


def ready_to_replay(args: argparse.Namespace) -> tuple[Episode, Criterion]:
    """The episode and its approved criterion, restoring its starting files first when they are missing."""
    episode = find_episode(args.projects, args.episode_id)
    criterion = load_criterion(args.runs, args.episode_id)
    shown = display_id(args.runs, args.episode_id)
    if criterion is None or not criterion.approved:
        symptom = read_symptom(args.runs, args.episode_id)
        raise NotReady(f"no approved criterion for {shown} — "
                       f"{criterion_step(criterion, shown, symptom.value if symptom else None)}")
    if not pristine_dir(args.runs, args.episode_id).is_dir():
        for line in restore_episode(args.runs, args.projects, args.episode_id):
            print(line)
    return episode, criterion


def run_baseline(args: argparse.Namespace, episode: Episode, criterion: Criterion) -> VariantOutcome:
    """Replay the unchanged episode N times under the episode cap and judge each run."""
    folder = episode_dir(args.runs, args.episode_id)
    spent = stored_spend(folder)
    show_plan(plan_cost([DEFAULT_REPLAY_BUDGET_USD], n=RUNS_PER_VARIANT, spent_usd=spent, cap_usd=EPISODE_CAP_USD))
    if retire_if_criterion_changed(folder, criterion):
        print("the criterion changed since the last runs: they were moved to superseded/ and will not be reused",
              file=sys.stderr)
    budget = EpisodeBudget(EPISODE_CAP_USD, spent_usd=spent)
    with ProgressPrinter() as progress:
        outcome = run_variant(BASELINE, baseline_settings(episode), n=RUNS_PER_VARIANT,
                              episode=episode, runs_dir=args.runs, criterion=criterion, budget=budget,
                              progress=progress)
    print(f"baseline: {outcome.passes} of {outcome.runs} passed, ${outcome.cost_usd:.2f} spent on replays")
    return outcome


def env_fix_command(args: argparse.Namespace) -> int:
    destination = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.script, destination)
    print(f"stored {destination}; the environment oracle runs it in each replay's clone before Claude starts")
    print_next(args.runs, args.episode_id)
    return 0


def diagnose_command(args: argparse.Namespace) -> int:
    if args.episode_id is None:
        return diagnose_queue(args)
    symptom = read_symptom(args.runs, args.episode_id)
    if symptom is None:
        raise NotReady(f"{display_id(args.runs, args.episode_id)} has no confirmed symptom — "
                       f"{confirm_step(args.runs, args.episode_id)}")
    episode, criterion = ready_to_replay(args)
    folder = episode_dir(args.runs, args.episode_id)
    env_script = env_fix_script(folder)
    budget = EpisodeBudget(EPISODE_CAP_USD, spent_usd=stored_spend(folder))
    show_plan(diagnose_cost_for(folder, criterion, DEFAULT_REPLAY_BUDGET_USD, model=episode.model, env_fix=env_script is not None,
                                n=RUNS_PER_VARIANT, spent_usd=budget.spent_usd, cap_usd=EPISODE_CAP_USD))
    with ProgressPrinter() as progress:
        verdict = diagnose(episode, criterion, symptom, baseline_settings(episode),
                           runs_dir=args.runs, follow_ups=follow_up_prompts(args.projects, args.episode_id),
                           env_script=env_script, n=RUNS_PER_VARIANT, budget=budget,
                           progress=progress)
    print(f"cause: {verdict.cause}\nevidence: {verdict.evidence}")
    if numbers := numbers_line(verdict):
        print(numbers)
    if limited := limits_line(verdict):
        print(limited)
    if verdict.pair:
        print(f"together: {' + '.join(verdict.pair)}")
    for cause, reason in verdict.untested.items():
        print(f"{cause} untested: {reason}")
    for line in trust_lines(args.runs):
        print(line)
    print_next(args.runs, args.episode_id)
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
    while (run_dir := next_unlabelled(args.runs, skip=skipped)) is not None:
        print(f"\n{run_id(args.runs, run_dir)}\n{run_summary(run_dir)}")
        answer = choose("your verdict?", ("pass", "fail", "skip", "quit"))
        if answer == "quit":
            break
        if answer == "skip":
            skipped.add(run_dir)
        else:
            record_label(args.runs, run_id(args.runs, run_dir), answer == "pass")
    else:
        print("every judged replay is labelled" + (f" or skipped ({len(skipped)} skipped)" if skipped else ""))
    print(trust_lines(args.runs)[0])
    return 0


def agreement_json(result) -> dict:
    return {"labelled": result.labelled, "agreed": result.agreed, "kappa": result.kappa,
            "calibrated": result.calibrated, "min_labels": MIN_LABELS, "min_kappa": MIN_KAPPA}


def selftest_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    baseline = load_outcome(episode_dir(args.runs, args.episode_id), BASELINE)
    if baseline is None:
        baseline = run_baseline(args, episode, criterion)
    if not is_control(args.runs, args.episode_id, RUNS_PER_VARIANT):
        print(f"culprit: {display_id(args.runs, args.episode_id)} is not a control — its baseline passed "
              f"{baseline.passes} of {baseline.runs}, and a control must pass at least "
              f"{control_min_passes(RUNS_PER_VARIANT)} of {RUNS_PER_VARIANT}",
              file=sys.stderr)
        return EXIT_REFUSED
    show_plan(selftest_cost(args.runs, args.episode_id, criterion, baseline_settings(episode),
                            n=RUNS_PER_VARIANT, budget_per_fault=FAULT_CAP_USD))
    with ProgressPrinter() as progress:
        results = run_selftest(episode, criterion, baseline_settings(episode), runs_dir=args.runs,
                               follow_ups=follow_up_prompts(args.projects, args.episode_id), n=RUNS_PER_VARIANT,
                               budget_per_fault=FAULT_CAP_USD, progress=progress)
    for result in results:
        print(f"{result.fault}: expected {result.expected}, got {result.got} {'✓' if result.correct else '✗'}")
    print(planted_line(sum(r.correct for r in results), len(results)))
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
    for episode_id in queued_ids(args.runs):
        approved, restored = _confirm_progress(args.runs, episode_id)
        if restored or not approved:
            continue
        shown = display_id(args.runs, episode_id)
        try:
            restore_episode(args.runs, args.projects, episode_id)
            print(f"restored {shown}")
        except (NotReplayable, EpisodeNotFound) as refusal:
            print(f"{shown} not restored: {refusal}")
    show_plan(batch_cost(args.runs, n=RUNS_PER_VARIANT, per_episode_usd=EPISODE_CAP_USD, total_usd=TOTAL_CAP_USD,
                         replay_budget_usd=DEFAULT_REPLAY_BUDGET_USD))
    with ProgressPrinter() as progress:
        summary = diagnose_all(args.runs, args.projects, n=RUNS_PER_VARIANT, per_episode_usd=EPISODE_CAP_USD,
                               total_usd=TOTAL_CAP_USD, replay_budget_usd=DEFAULT_REPLAY_BUDGET_USD, progress=progress)
    print(f"diagnosed {len(summary.diagnosed)}: {', '.join(summary.diagnosed) or 'none'}")
    for episode_id in summary.diagnosed:
        verdict = load_verdict(episode_dir(args.runs, episode_id))
        if verdict and (numbers := numbers_line(verdict)):
            print(f"{display_id(args.runs, episode_id)} {numbers}")
    for episode_id, reason in summary.skipped.items():
        print(f"{episode_id} skipped: {reason}")
    for episode_id in summary.over_cap:
        print(f"{episode_id} hit its ${EPISODE_CAP_USD:.2f} cap; its finished runs are kept")
    if summary.stopped_at:
        print(f"stopped at {summary.stopped_at}: the ${TOTAL_CAP_USD:.2f} total is spent — rerun to continue from there")
    write_report(args.runs)
    print(f"spent ${summary.spent_usd:.2f}; report written to {args.runs / REPORT_FILE}")
    return 0
