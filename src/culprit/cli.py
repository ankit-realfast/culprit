"""The culprit command line. Each subcommand is one handler: handler(args) -> exit code."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from culprit.batch import ENV_FIX_FILE, diagnose_all
from culprit.calibration import (MIN_KAPPA, MIN_LABELS, UnknownRun, agreement, next_unlabelled, record_label,
                                 run_id, run_summary)
from culprit.claude import ClaudeCallFailed, ask_claude
from culprit.criterion import Criterion, approve_criterion, criterion_step, draft_criterion, load_criterion, save_criterion
from culprit.diagnose import diagnose, retire_if_criterion_changed
from culprit.eligibility import project_hooks, replay_blockers
from culprit.episodes import Episode, episodes_from_session, session_files
from culprit.experiment import PRISTINE_DIR, BudgetExhausted, EpisodeBudget, load_outcome, run_variant, stored_spend
from culprit.locate import EpisodeNotFound, find_episode, follow_up_prompts
from culprit.preview import CostPlan, batch_cost, diagnose_cost_for, plan_cost, selftest_cost
from culprit.progress import ProgressPrinter
from culprit.replay import DEFAULT_REPLAY_BUDGET_USD, OriginalModified, SetupScriptFailed, baseline_settings
from culprit.report import REPORT_FILE, RESTORE_FILE, collect, render_report
from culprit.sandbox import (DEFAULT_FILE_HISTORY, apply_episode_originals, check_restore, episode_originals, restore,
                             restore_from_git)
from culprit.selftest import CONTROL_MIN_PASSES, is_control, run_selftest, selftest_accuracy
from culprit.signals import FailureSignals
from culprit.snapshot import load_snapshot, session_root
from culprit.status import EpisodeStatus, episode_status, next_step, queue_status
from culprit.store import (NotReady, UnknownEpisode, candidate_rows, confirm_step, episode_dir, read_symptom, record_symptom,
                           resolve_episode_id, save_candidates, short_id)
from culprit.taxonomy import Symptom
from culprit.triage import EpisodeSummary, rank_candidates

DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
RUNS_ENV = "CULPRIT_RUNS"
EXIT_REFUSED = 1
EXIT_NOT_READY = 3
EXIT_BUDGET = 4
EXIT_INTERRUPTED = 130


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            SetupScriptFailed, UnknownRun, OriginalModified) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return EXIT_REFUSED


def default_runs() -> Path:
    """The store: $CULPRIT_RUNS when set, otherwise runs/ in the current folder; --runs overrides both."""
    return Path(os.environ.get(RUNS_ENV) or "runs")


def require_store(runs_dir: Path) -> None:
    """Read-only commands call this so a wrong folder is reported instead of an empty store being created."""
    if not runs_dir.is_dir():
        raise NotReady(f"no culprit store at {runs_dir.resolve()} — run from the folder holding runs/ "
                       f"or set {RUNS_ENV}")


COMMAND_GROUPS = {
    "workflow": ("triage", "restore", "baseline", "env-fix", "diagnose", "report", "status"),
    "human review": ("confirm", "criterion"),
    "trust": ("review", "label", "calibration", "selftest"),
    "batch": ("diagnose-all",),
}
EXAMPLE_RUN = """example run:
  culprit triage --top 20                  rank likely failures; rows are numbered #1, #2, ...
  culprit confirm #1 wrong-result          say what went wrong
  culprit criterion draft #1               draft what success means, then edit it
  culprit criterion approve #1
  culprit restore #1                       rebuild the starting files
  culprit baseline #1 --n 1 --budget 3     one cheap unchanged replay first
  culprit diagnose #1                      find the layer to change
  culprit report                           rank the changes across episodes
  culprit status                           where every episode stands, and what to run next
"""
EXAMPLES = {
    "triage": "  culprit triage --top 40\n  culprit triage --projects ~/.claude/projects --runs ./runs\n",
    "confirm": "  culprit confirm #1 wrong-result\n  culprit confirm 1a2b3c4d:5e6f7a8b no-failure\n",
    "status": "  culprit status\n  culprit status #1\n",
    "criterion": "  culprit criterion draft #1\n  culprit criterion show #1\n  culprit criterion approve #1\n"
                 "  culprit criterion draft #1 --force     replace an existing criterion\n",
    "restore": "  culprit restore #1\n  culprit restore #1 --dest /tmp/look   rebuild somewhere else to inspect\n",
    "baseline": "  culprit baseline #1 --dry-run         show the most it could run and spend\n"
                "  culprit baseline #1 --n 1 --budget 3\n  culprit baseline #1 --yes             no question asked\n",
    "env-fix": "  culprit env-fix #1 fix-env.sh         run before Claude starts in the environment oracle\n",
    "diagnose": "  culprit diagnose #1 --dry-run\n  culprit diagnose #1 --budget 60\n",
    "review": "  culprit review\n",
    "label": "  culprit label 1a2b__5e6f/replays/baseline/0 pass\n",
    "calibration": "  culprit calibration\n",
    "selftest": "  culprit selftest #4 --dry-run\n  culprit selftest #4 --budget-per-fault 20\n",
    "report": "  culprit report\n  CULPRIT_RUNS=~/work/runs culprit report\n",
    "diagnose-all": "  culprit diagnose-all --dry-run\n  culprit diagnose-all --per-episode 40 --total 400 --yes\n",
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
    _projects_and_runs(triage)
    triage.add_argument("--top", type=int, default=20)
    triage.set_defaults(handler=triage_command)

    confirm = add("confirm", "record what went wrong in a queued episode")
    confirm.add_argument("episode_id")
    confirm.add_argument("symptom", choices=[s.value for s in Symptom])
    confirm.add_argument("--runs", type=Path, default=default_runs())
    confirm.set_defaults(handler=confirm_command, needs_store=True)

    status = add("status", "where each episode stands and the command that moves it on")
    status.add_argument("episode_id", nargs="?")
    status.add_argument("--runs", type=Path, default=default_runs())
    status.set_defaults(handler=status_command, needs_store=True)

    criterion = add("criterion", "draft, show or approve what success means for an episode")
    criterion.add_argument("action", choices=["draft", "show", "approve"])
    criterion.add_argument("episode_id")
    criterion.add_argument("--force", action="store_true", help="draft: replace an existing criterion")
    _projects_and_runs(criterion)
    criterion.set_defaults(handler=criterion_command)

    restore_parser = add("restore", "rebuild an episode's starting files and check them")
    restore_parser.add_argument("episode_id")
    _projects_and_runs(restore_parser)
    restore_parser.add_argument("--file-history", type=Path, default=DEFAULT_FILE_HISTORY)
    restore_parser.add_argument("--dest", type=Path, default=None)
    restore_parser.set_defaults(handler=restore_command)

    baseline = add("baseline", "replay the unchanged episode N times and judge each run")
    baseline.add_argument("episode_id")
    _projects_and_runs(baseline)
    baseline.add_argument("--n", type=int, default=5)
    baseline.add_argument("--budget", type=float, default=10.0, help="spend cap in USD for this episode, counting what it already spent")
    baseline.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    _spend_flags(baseline)
    baseline.set_defaults(handler=baseline_command)

    env_fix = add("env-fix", "supply a script that fixes the environment for the environment oracle")
    env_fix.add_argument("episode_id")
    env_fix.add_argument("script", type=Path)
    env_fix.add_argument("--runs", type=Path, default=default_runs())
    env_fix.set_defaults(handler=env_fix_command)

    diagnose_parser = add("diagnose", "find what to change: run the oracles and decide the cause")
    diagnose_parser.add_argument("episode_id")
    _projects_and_runs(diagnose_parser)
    diagnose_parser.add_argument("--n", type=int, default=5)
    diagnose_parser.add_argument("--budget", type=float, default=40.0, help="spend cap in USD for this episode, counting what it already spent")
    diagnose_parser.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    _spend_flags(diagnose_parser)
    diagnose_parser.set_defaults(handler=diagnose_command)

    review = add("review", "show the next judged replay you have not labelled")
    review.add_argument("--runs", type=Path, default=default_runs())
    review.set_defaults(handler=review_command, needs_store=True)

    label = add("label", "record your own pass/fail for one judged replay")
    label.add_argument("run")
    label.add_argument("verdict", choices=["pass", "fail"])
    label.add_argument("--runs", type=Path, default=default_runs())
    label.set_defaults(handler=label_command)

    calibration = add("calibration", "how often the judge agrees with your labels")
    calibration.add_argument("--runs", type=Path, default=default_runs())
    calibration.set_defaults(handler=calibration_command, needs_store=True)

    selftest = add("selftest", "plant known faults into a control episode and score culprit")
    selftest.add_argument("episode_id")
    _projects_and_runs(selftest)
    selftest.add_argument("--n", type=int, default=5)
    selftest.add_argument("--budget-per-fault", type=float, default=40.0)
    selftest.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    _spend_flags(selftest)
    selftest.set_defaults(handler=selftest_command)

    report = add("report", "write runs/report.md from every stored verdict")
    report.add_argument("--runs", type=Path, default=default_runs())
    report.set_defaults(handler=report_command, needs_store=True)

    batch = add("diagnose-all", "diagnose every prepared episode in the queue, then report")
    _projects_and_runs(batch)
    batch.add_argument("--n", type=int, default=5)
    batch.add_argument("--per-episode", type=float, default=40.0)
    batch.add_argument("--total", type=float, default=400.0)
    batch.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    _spend_flags(batch)
    batch.set_defaults(handler=diagnose_all_command)
    width = max(map(len, summaries)) + 2
    parser.description = "Find what to change when a Claude Code episode fails.\n" + "".join(
        f"\n{group}:\n" + "".join(f"  {name:<{width}}{summaries[name]}\n" for name in names)
        for group, names in COMMAND_GROUPS.items())
    return parser


def _projects_and_runs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument("--runs", type=Path, default=default_runs())


def _spend_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="print the most it could run and spend, then stop")
    parser.add_argument("--yes", "-y", action="store_true", help="run without asking to confirm the spend")


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def approve_spend(args: argparse.Namespace, plan: CostPlan) -> int | None:
    """Show the most a command can spend and get a yes; None to go on, otherwise the exit code to stop with."""
    if args.dry_run:
        for line in plan.lines():
            print(line)
        return 0
    for line in plan.lines():
        print(line, file=sys.stderr)
    if args.yes:
        return None
    if not stdin_is_tty():
        print("culprit: not started — no terminal to confirm the spend; pass --yes to run without asking",
              file=sys.stderr)
        return EXIT_NOT_READY
    try:
        answer = input("proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return None
    print("culprit: not started", file=sys.stderr)
    return EXIT_REFUSED


def triage_command(args: argparse.Namespace) -> int:
    summaries = [
        EpisodeSummary.from_episode(episode)
        for session_file in session_files(args.projects)
        for episode in episodes_from_session(session_file)
    ]
    candidates = rank_candidates(summaries, args.top)
    path = save_candidates(args.runs, candidates)
    queue = [row["summary"]["episode_id"] for row in candidate_rows(args.runs)]
    for candidate in candidates:
        s = candidate.summary
        row = f"#{queue.index(s.episode_id) + 1}"
        print(f"{row:>4} {candidate.score:>3}  {short_id(s.episode_id)}  {s.episode_id}  "
              f"[{_evidence(s.signals)}]  {s.prompt_excerpt}")
        if s.signals.api_errors:
            print(f"            {s.signals.api_errors} API errors — likely not reproducible")
        blockers = _blockers_for(args.projects, s.episode_id)
        if blockers:
            print(f"            not replayable: {'; '.join(blockers)}")
    print(f"{len(summaries)} episodes scanned; top {len(candidates)} merged into {path} ({len(queue)} queued); "
          f"refer to an episode by its #row, short id or any unique prefix")
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
    path = record_symptom(args.runs, args.episode_id, Symptom(args.symptom))
    print(f"recorded {args.symptom} for {args.episode_id} in {path}")
    print_next(args.runs, args.episode_id)
    return 0


def print_next(runs_dir: Path, episode_id: str) -> None:
    """Every state-changing command ends by naming the step that follows, as `culprit status` would."""
    print(f"next: {next_step(runs_dir, episode_id)}", file=sys.stderr)


def status_command(args: argparse.Namespace) -> int:
    if args.episode_id:
        for line in status_lines(episode_status(args.runs, args.episode_id)):
            print(line)
        return 0
    statuses = queue_status(args.runs)
    if not statuses:
        print("nothing queued — run `culprit triage`")
    for status in statuses:
        print(f"{short_id(status.episode_id)}  {status.stage:<18}  next: {status.next_step}")
    return 0


def status_lines(status: EpisodeStatus) -> list[str]:
    lines = [status.episode_id, f"stage: {status.stage}", f"symptom: {status.symptom or 'not confirmed'}"]
    if status.criterion is None:
        lines.append("criterion: none")
    else:
        state = "approved" if status.criterion.approved else "draft, not approved"
        lines.append(f"criterion [{state}]: {status.criterion.success_means}")
    if status.restore:
        r = status.restore
        lines.append(f"restore: snapshot agrees for {r.get('snapshot_agreed', 0)} of {r.get('checked', 0)} files, "
                     f"{r.get('unknown', 0)} unknown, {r.get('missing_backups', 0)} missing backups")
    for outcome in status.outcomes:
        void = f", {outcome.void} void" if outcome.void else ""
        lines.append(f"{outcome.variant}: {outcome.passes} of {outcome.runs} passed{void}, ${outcome.cost_usd:.2f}")
    lines.append(f"spent: ${status.spent_usd:.2f}")
    if status.verdict:
        lines.append(f"cause: {status.verdict.cause} — {status.verdict.evidence}")
    lines.append(f"next: {status.next_step}")
    return lines


def criterion_command(args: argparse.Namespace) -> int:
    if args.action == "draft":
        if load_criterion(args.runs, args.episode_id) is not None and not args.force:
            print(f"culprit: {args.episode_id} already has a criterion — view it with `culprit criterion show "
                  f"{args.episode_id}`; `--force` replaces it with a new unapproved draft", file=sys.stderr)
            return EXIT_REFUSED
        episode = find_episode(args.projects, args.episode_id)
        follow_ups = follow_up_prompts(args.projects, args.episode_id)
        path = save_criterion(args.runs, args.episode_id, draft_criterion(episode, follow_ups, ask=ask_claude))
        print(f"drafted {path} — edit it if needed before approving")
        print_next(args.runs, args.episode_id)
        return 0
    if args.action == "approve":
        approve_criterion(args.runs, args.episode_id)
        print(f"approved the criterion for {args.episode_id}")
        print_next(args.runs, args.episode_id)
        return 0
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None:
        raise NotReady(f"no criterion for {args.episode_id} — run `culprit criterion draft {args.episode_id}` first")
    state = "approved" if criterion.approved else "draft, not approved"
    print(f"[{state}]\nsuccess means: {criterion.success_means}")
    for check in criterion.checks:
        print(f"check: {check}")
    for rule in criterion.must_not:
        print(f"must not: {rule}")
    return 0


def restore_command(args: argparse.Namespace) -> int:
    episode = find_episode(args.projects, args.episode_id)
    tracked = load_snapshot(episode)
    blockers = replay_blockers(episode, tracked)
    if blockers:
        print(f"culprit: {args.episode_id} cannot be replayed: " + "; ".join(blockers), file=sys.stderr)
        return 1
    destination = args.dest or episode_dir(args.runs, args.episode_id) / PRISTINE_DIR
    if destination.exists():
        print(f"culprit: {destination} already exists — delete it to restore again", file=sys.stderr)
        return 1
    report = restore(episode, tracked, destination, file_history_root=args.file_history,
                     session_root=session_root(episode.session_file))
    root = session_root(episode.session_file) or Path(episode.cwd)
    covered = {str(Path(path) if Path(path).is_absolute() else root / path) for path in tracked}
    covered |= set(episode_originals(episode))
    from_git = restore_from_git(episode, destination, covered)
    checked, mismatches = check_restore(episode, destination)  # the snapshot, before the episode's record overrides it
    written, removed = apply_episode_originals(episode, destination)
    print(f"{report.restored} restored from snapshots, {len(report.unknown)} unknown (snapshot kept no backup), "
          f"{len(report.missing_backups)} missing backups, {len(report.outside_cwd)} outside the working directory "
          f"→ {destination}")
    print(f"snapshot agrees with the episode's own record for {checked - len(mismatches)} of {checked} files")
    for mismatch in mismatches:
        print(f"  mismatch: {mismatch.path} ({mismatch.reason})")
    print(f"{written} file(s) set from the episode's own record, {removed} removed (created during the episode)")
    if from_git:
        print(f"{from_git.restored} other file(s) set from git commit {from_git.commit[:10]} (the last before the "
              f"episode), {from_git.removed} removed (added after it)")
    trust = {"git_commit": from_git.commit if from_git else None,
             "set_from_git": from_git.restored if from_git else 0,
             "removed_by_git": from_git.removed if from_git else 0,"snapshot_agreed": checked - len(mismatches), "checked": checked, "restored": report.restored,
             "unknown": len(report.unknown), "missing_backups": len(report.missing_backups),
             "outside_cwd": len(report.outside_cwd), "set_from_episode": written, "removed": removed}
    episode_dir(args.runs, args.episode_id).mkdir(parents=True, exist_ok=True)
    (episode_dir(args.runs, args.episode_id) / RESTORE_FILE).write_text(json.dumps(trust, indent=2))
    hooks = project_hooks(Path(episode.cwd))
    print("project hooks that will run in every replay: " + "; ".join(hooks) if hooks else "no project hooks")
    print_next(args.runs, args.episode_id)
    return 0


def ready_to_replay(args: argparse.Namespace) -> tuple[Episode, Criterion]:
    """The episode and its approved criterion, once restore has built the pristine copy; refuses otherwise."""
    episode = find_episode(args.projects, args.episode_id)
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None or not criterion.approved:
        raise NotReady(f"no approved criterion for {args.episode_id} — {criterion_step(criterion, args.episode_id)}")
    if not (episode_dir(args.runs, args.episode_id) / PRISTINE_DIR).is_dir():
        raise NotReady(f"{args.episode_id} has no restored copy — run `culprit restore {args.episode_id}` first")
    return episode, criterion


def baseline_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    spent = stored_spend(episode_dir(args.runs, args.episode_id))
    stop = approve_spend(args, plan_cost([args.replay_budget], n=args.n, spent_usd=spent, cap_usd=args.budget))
    if stop is not None:
        return stop  # asked before retiring stale runs, so a dry run or a "no" changes nothing on disk
    if retire_if_criterion_changed(episode_dir(args.runs, args.episode_id), criterion):
        print("the criterion changed since the last runs: they were moved to superseded/ and will not be reused")
    budget = EpisodeBudget(args.budget, spent_usd=spent)
    with ProgressPrinter() as progress:
        outcome = run_variant("baseline", baseline_settings(episode, args.replay_budget), n=args.n, episode=episode,
                              runs_dir=args.runs, criterion=criterion, budget=budget, progress=progress)
    print(f"baseline: {outcome.passes} of {outcome.runs} passed, ${outcome.cost_usd:.2f} spent on replays")
    print_next(args.runs, args.episode_id)
    return 0




def env_fix_command(args: argparse.Namespace) -> int:
    destination = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.script, destination)
    print(f"stored {destination}; the environment oracle runs it in each replay's clone before Claude starts")
    print_next(args.runs, args.episode_id)
    return 0


def diagnose_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    symptom = read_symptom(args.runs, args.episode_id)
    if symptom is None:
        raise NotReady(f"{args.episode_id} has no confirmed symptom — {confirm_step(args.runs, args.episode_id)}")
    folder = episode_dir(args.runs, args.episode_id)
    env_script = folder / ENV_FIX_FILE
    budget = EpisodeBudget(args.budget, spent_usd=stored_spend(folder))
    stop = approve_spend(args, diagnose_cost_for(folder, criterion, args.replay_budget, model=episode.model,
                                                 env_fix=env_script.exists(), n=args.n,
                                                 spent_usd=budget.spent_usd, cap_usd=args.budget))
    if stop is not None:
        return stop
    with ProgressPrinter() as progress:
        verdict = diagnose(episode, criterion, symptom, baseline_settings(episode, args.replay_budget),
                           runs_dir=args.runs, follow_ups=follow_up_prompts(args.projects, args.episode_id),
                           env_script=env_script if env_script.exists() else None, n=args.n, budget=budget,
                           progress=progress)
    print(f"cause: {verdict.cause}\nevidence: {verdict.evidence}")
    if verdict.pair:
        print(f"together: {' + '.join(verdict.pair)}")
    for cause, reason in verdict.untested.items():
        print(f"{cause} untested: {reason}")
    for line in trust_lines(args.runs):
        print(line)
    print_next(args.runs, args.episode_id)
    return 0


def trust_lines(runs_dir: Path) -> list[str]:
    result = agreement(runs_dir)
    kappa = "not computable yet" if result.kappa is None else f"{result.kappa:.2f}"
    state = "calibrated" if result.calibrated else f"uncalibrated (needs {MIN_LABELS} labels and kappa ≥ {MIN_KAPPA})"
    correct, total = selftest_accuracy(runs_dir)
    planted = f"selftest: {correct} of {total} planted faults named correctly" if total else "selftest: not run"
    return [f"judge: {result.agreed} of {result.labelled} labels agree, kappa {kappa} — {state}", planted]


def review_command(args: argparse.Namespace) -> int:
    run_dir = next_unlabelled(args.runs)
    if run_dir is None:
        print("every judged replay is labelled")
        return 0
    print(f"{run_id(args.runs, run_dir)}\n{run_summary(run_dir)}")
    print(f"\nlabel it: culprit label {run_id(args.runs, run_dir)} pass|fail")
    return 0


def label_command(args: argparse.Namespace) -> int:
    record_label(args.runs, args.run, args.verdict == "pass")
    print(f"labelled {args.run} {args.verdict}")
    more = next_unlabelled(args.runs) is not None
    print(f"next: run `culprit {'review' if more else 'calibration'}`", file=sys.stderr)
    return 0


def calibration_command(args: argparse.Namespace) -> int:
    result = agreement(args.runs)
    kappa = "not computable yet" if result.kappa is None else f"{result.kappa:.2f}"
    print(f"{result.labelled} labelled, {result.agreed} agree with the judge, kappa {kappa}")
    print("calibrated" if result.calibrated else f"uncalibrated: needs {MIN_LABELS} labels and kappa ≥ {MIN_KAPPA}")
    return 0


def selftest_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    if not is_control(args.runs, args.episode_id):
        baseline = load_outcome(episode_dir(args.runs, args.episode_id), "baseline")
        if baseline is None:
            raise NotReady(f"{args.episode_id} has no baseline — run `culprit baseline {args.episode_id}` first; "
                           f"a control must pass at least {CONTROL_MIN_PASSES} of 5")
        print(f"culprit: {args.episode_id} is not a control — its baseline passed {baseline.passes} of "
              f"{baseline.runs}, and a control must pass at least {CONTROL_MIN_PASSES} of 5", file=sys.stderr)
        return EXIT_REFUSED
    stop = approve_spend(args, selftest_cost(args.runs, args.episode_id, criterion,
                                             baseline_settings(episode, args.replay_budget), n=args.n,
                                             budget_per_fault=args.budget_per_fault))
    if stop is not None:
        return stop
    with ProgressPrinter() as progress:
        results = run_selftest(episode, criterion, baseline_settings(episode, args.replay_budget), runs_dir=args.runs,
                               follow_ups=follow_up_prompts(args.projects, args.episode_id), n=args.n,
                               budget_per_fault=args.budget_per_fault, progress=progress)
    for result in results:
        print(f"{result.fault}: expected {result.expected}, got {result.got} {'✓' if result.correct else '✗'}")
    print(f"{sum(r.correct for r in results)} of {len(results)} planted faults named correctly")
    return 0


def write_report(runs_dir: Path) -> str:
    text = render_report(collect(runs_dir), trust_lines(runs_dir))
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / REPORT_FILE).write_text(text)
    return text


def report_command(args: argparse.Namespace) -> int:
    print(write_report(args.runs), end="")
    return 0


def diagnose_all_command(args: argparse.Namespace) -> int:
    stop = approve_spend(args, batch_cost(args.runs, n=args.n, per_episode_usd=args.per_episode,
                                          total_usd=args.total, replay_budget_usd=args.replay_budget))
    if stop is not None:
        return stop
    with ProgressPrinter() as progress:
        summary = diagnose_all(args.runs, args.projects, n=args.n, per_episode_usd=args.per_episode,
                               total_usd=args.total, replay_budget_usd=args.replay_budget, progress=progress)
    print(f"diagnosed {len(summary.diagnosed)}: {', '.join(summary.diagnosed) or 'none'}")
    for episode_id, reason in summary.skipped.items():
        print(f"{episode_id} skipped: {reason}")
    for episode_id in summary.over_cap:
        print(f"{episode_id} hit its ${args.per_episode:.2f} cap — rerun with a larger --per-episode to resume it")
    if summary.stopped_at:
        print(f"stopped at {summary.stopped_at}: the ${args.total:.2f} total is spent — rerun to continue from there")
    write_report(args.runs)
    print(f"spent ${summary.spent_usd:.2f}; report written to {args.runs / REPORT_FILE}")
    return 0
