"""The culprit command line: `triage` builds the review queue, `confirm` records the human's label."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from culprit.episodes import episodes_from_session, session_files
from culprit.signals import FailureSignals
from culprit.store import UnknownEpisode, record_symptom, save_candidates
from culprit.taxonomy import Symptom
from culprit.triage import EpisodeSummary, rank_candidates

DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_RUNS = Path("runs")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "triage":
        return _triage(args.projects, args.runs, args.top)
    return _confirm(args.runs, args.episode_id, Symptom(args.symptom))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", description="Find what to change when a Claude Code episode fails.")
    commands = parser.add_subparsers(dest="command", required=True)
    triage = commands.add_parser("triage", help="rank episodes most likely to have failed")
    triage.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    triage.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    triage.add_argument("--top", type=int, default=20)
    confirm = commands.add_parser("confirm", help="record what went wrong in a queued episode")
    confirm.add_argument("episode_id")
    confirm.add_argument("symptom", choices=[s.value for s in Symptom])
    confirm.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    return parser


def _triage(projects: Path, runs: Path, top: int) -> int:
    summaries = [
        EpisodeSummary.from_episode(episode)
        for session_file in session_files(projects)
        for episode in episodes_from_session(session_file)
    ]
    candidates = rank_candidates(summaries, top)
    path = save_candidates(runs, candidates)
    for candidate in candidates:
        s = candidate.summary
        print(f"{candidate.score:>3}  {s.episode_id}  [{_evidence(s.signals)}]  {s.prompt_excerpt}")
    print(f"{len(summaries)} episodes scanned; top {len(candidates)} saved to {path}")
    return 0


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


def _confirm(runs: Path, episode_id: str, symptom: Symptom) -> int:
    try:
        path = record_symptom(runs, episode_id, symptom)
    except (UnknownEpisode, FileNotFoundError) as refusal:
        print(f"culprit: {refusal}", file=sys.stderr)
        return 1
    print(f"recorded {symptom.value} for {episode_id} in {path}")
    return 0
