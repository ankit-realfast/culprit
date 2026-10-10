"""The culprit command line. Each subcommand is one handler: handler(args) -> exit code."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from culprit.claude import ClaudeCallFailed, ask_claude
from culprit.criterion import Criterion, approve_criterion, draft_criterion, load_criterion, save_criterion
from culprit.diagnose import diagnose
from culprit.eligibility import project_hooks, replay_blockers
from culprit.episodes import Episode, episodes_from_session, session_files
from culprit.experiment import PRISTINE_DIR, BudgetExhausted, EpisodeBudget, run_variant
from culprit.locate import EpisodeNotFound, find_episode, follow_up_prompts
from culprit.replay import DEFAULT_REPLAY_BUDGET_USD, SetupScriptFailed, baseline_settings
from culprit.sandbox import DEFAULT_FILE_HISTORY, apply_episode_originals, check_restore, restore
from culprit.signals import FailureSignals
from culprit.snapshot import load_snapshot, session_root
from culprit.store import UnknownEpisode, episode_dir, read_symptom, record_symptom, save_candidates
from culprit.taxonomy import Symptom
from culprit.triage import EpisodeSummary, rank_candidates
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_RUNS = Path("runs")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (UnknownEpisode, EpisodeNotFound, FileNotFoundError, ClaudeCallFailed, BudgetExhausted,
            SetupScriptFailed) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", description="Find what to change when a Claude Code episode fails.")
    commands = parser.add_subparsers(dest="command", required=True)

    triage = commands.add_parser("triage", help="rank episodes most likely to have failed")
    _projects_and_runs(triage)
    triage.add_argument("--top", type=int, default=20)
    triage.set_defaults(handler=triage_command)

    confirm = commands.add_parser("confirm", help="record what went wrong in a queued episode")
    confirm.add_argument("episode_id")
    confirm.add_argument("symptom", choices=[s.value for s in Symptom])
    confirm.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    confirm.set_defaults(handler=confirm_command)

    criterion = commands.add_parser("criterion", help="draft, show or approve what success means for an episode")
    criterion.add_argument("action", choices=["draft", "show", "approve"])
    criterion.add_argument("episode_id")
    _projects_and_runs(criterion)
    criterion.set_defaults(handler=criterion_command)

    restore_parser = commands.add_parser("restore", help="rebuild an episode's starting files and check them")
    restore_parser.add_argument("episode_id")
    _projects_and_runs(restore_parser)
    restore_parser.add_argument("--file-history", type=Path, default=DEFAULT_FILE_HISTORY)
    restore_parser.add_argument("--dest", type=Path, default=None)
    restore_parser.set_defaults(handler=restore_command)

    baseline = commands.add_parser("baseline", help="replay the unchanged episode N times and judge each run")
    baseline.add_argument("episode_id")
    _projects_and_runs(baseline)
    baseline.add_argument("--n", type=int, default=5)
    baseline.add_argument("--budget", type=float, default=10.0, help="spend cap in USD for this command")
    baseline.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    baseline.set_defaults(handler=baseline_command)

    env_fix = commands.add_parser("env-fix", help="supply a script that fixes the environment for the environment oracle")
    env_fix.add_argument("episode_id")
    env_fix.add_argument("script", type=Path)
    env_fix.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    env_fix.set_defaults(handler=env_fix_command)

    diagnose_parser = commands.add_parser("diagnose", help="find what to change: run the oracles and decide the cause")
    diagnose_parser.add_argument("episode_id")
    _projects_and_runs(diagnose_parser)
    diagnose_parser.add_argument("--n", type=int, default=5)
    diagnose_parser.add_argument("--budget", type=float, default=40.0, help="spend cap in USD for this episode")
    diagnose_parser.add_argument("--replay-budget", type=float, default=DEFAULT_REPLAY_BUDGET_USD)
    diagnose_parser.set_defaults(handler=diagnose_command)
    return parser


def _projects_and_runs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)


def triage_command(args: argparse.Namespace) -> int:
    summaries = [
        EpisodeSummary.from_episode(episode)
        for session_file in session_files(args.projects)
        for episode in episodes_from_session(session_file)
    ]
    candidates = rank_candidates(summaries, args.top)
    path = save_candidates(args.runs, candidates)
    for candidate in candidates:
        s = candidate.summary
        print(f"{candidate.score:>3}  {s.episode_id}  [{_evidence(s.signals)}]  {s.prompt_excerpt}")
        blockers = _blockers_for(args.projects, s.episode_id)
        if blockers:
            print(f"       not replayable: {'; '.join(blockers)}")
    print(f"{len(summaries)} episodes scanned; top {len(candidates)} saved to {path}")
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
    return 0


def criterion_command(args: argparse.Namespace) -> int:
    if args.action == "draft":
        episode = find_episode(args.projects, args.episode_id)
        follow_ups = follow_up_prompts(args.projects, args.episode_id)
        path = save_criterion(args.runs, args.episode_id, draft_criterion(episode, follow_ups, ask=ask_claude))
        print(f"drafted {path} — edit it if needed, then run `culprit criterion approve {args.episode_id}`")
        return 0
    if args.action == "approve":
        approve_criterion(args.runs, args.episode_id)
        print(f"approved the criterion for {args.episode_id}")
        return 0
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None:
        raise FileNotFoundError(f"no criterion for {args.episode_id} — run `culprit criterion draft {args.episode_id}` first")
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
    checked, mismatches = check_restore(episode, destination)  # the snapshot, before the episode's record overrides it
    written, removed = apply_episode_originals(episode, destination)
    print(f"{report.restored} restored from snapshots, {len(report.unknown)} unknown (snapshot kept no backup), "
          f"{len(report.missing_backups)} missing backups, {len(report.outside_cwd)} outside the working directory "
          f"→ {destination}")
    print(f"snapshot agrees with the episode's own record for {checked - len(mismatches)} of {checked} files")
    for mismatch in mismatches:
        print(f"  mismatch: {mismatch.path} ({mismatch.reason})")
    print(f"{written} file(s) set from the episode's own record, {removed} removed (created during the episode)")
    hooks = project_hooks(Path(episode.cwd))
    print("project hooks that will run in every replay: " + "; ".join(hooks) if hooks else "no project hooks")
    return 0


def ready_to_replay(args: argparse.Namespace) -> tuple[Episode, Criterion]:
    """The episode and its approved criterion, once restore has built the pristine copy; refuses otherwise."""
    episode = find_episode(args.projects, args.episode_id)
    criterion = load_criterion(args.runs, args.episode_id)
    if criterion is None or not criterion.approved:
        raise FileNotFoundError(f"no approved criterion for {args.episode_id} — run "
                                f"`culprit criterion draft {args.episode_id}` then `culprit criterion approve {args.episode_id}`")
    if not (episode_dir(args.runs, args.episode_id) / PRISTINE_DIR).is_dir():
        raise FileNotFoundError(f"{args.episode_id} has no restored copy — run `culprit restore {args.episode_id}` first")
    return episode, criterion


def baseline_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    outcome = run_variant("baseline", baseline_settings(episode, args.replay_budget), n=args.n, episode=episode,
                          runs_dir=args.runs, criterion=criterion, budget=EpisodeBudget(args.budget))
    print(f"baseline: {outcome.passes} of {outcome.runs} passed, ${outcome.cost_usd:.2f} spent on replays")
    return 0


ENV_FIX_FILE = "env-fix.sh"


def env_fix_command(args: argparse.Namespace) -> int:
    destination = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.script, destination)
    print(f"stored {destination}; the environment oracle runs it in each replay's clone before Claude starts")
    return 0


def diagnose_command(args: argparse.Namespace) -> int:
    episode, criterion = ready_to_replay(args)
    symptom = read_symptom(args.runs, args.episode_id)
    if symptom is None:
        raise FileNotFoundError(f"{args.episode_id} has no confirmed symptom — run `culprit confirm {args.episode_id} <symptom>` first")
    env_script = episode_dir(args.runs, args.episode_id) / ENV_FIX_FILE
    verdict = diagnose(episode, criterion, symptom, baseline_settings(episode, args.replay_budget),
                       runs_dir=args.runs, follow_ups=follow_up_prompts(args.projects, args.episode_id),
                       env_script=env_script if env_script.exists() else None, n=args.n,
                       budget=EpisodeBudget(args.budget))
    print(f"cause: {verdict.cause}\nevidence: {verdict.evidence}")
    if verdict.pair:
        print(f"together: {' + '.join(verdict.pair)}")
    for cause, reason in verdict.untested.items():
        print(f"{cause} untested: {reason}")
    return 0
