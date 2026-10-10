"""Diagnose one episode: baseline, then each oracle in cause order until one beats the baseline,
then the pair test. Every built variant and every outcome is saved, so a run stopped by the budget
resumes where it stopped instead of paying again.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from itertools import combinations
from pathlib import Path

from culprit.claude import ask_claude
from culprit.criterion import Criterion
from culprit.episodes import Episode
from culprit.experiment import PRISTINE_DIR, EpisodeBudget, VariantOutcome, load_outcome, run_variant
from culprit.oracles import build_variant, combine
from culprit.replay import ReplaySettings, settings_from_json, settings_to_json
from culprit.store import episode_dir
from culprit.taxonomy import ORACLE_ORDER, Cause, Symptom
from culprit.verdict import Verdict, beats, reproducible, save_verdict

VARIANTS_DIR = "variants"
BASELINE = "baseline"
COMBINED = "combined"
NOT_RUN_REASON = "not run: an earlier cause already explained the failure"


def diagnose(episode: Episode, criterion: Criterion, symptom: Symptom, base: ReplaySettings, *, runs_dir: Path,
             follow_ups: list[str], env_script: Path | None, n: int, budget: EpisodeBudget,
             out_dir: Path | None = None, ask=ask_claude, run=run_variant, build=build_variant) -> Verdict:
    folder = out_dir or episode_dir(runs_dir, episode.episode_id)
    pristine = episode_dir(runs_dir, episode.episode_id) / PRISTINE_DIR
    outcomes: dict[str, VariantOutcome] = {}
    untested: dict[str, str] = {}

    def measured(name: str, settings: ReplaySettings) -> VariantOutcome:
        saved = load_outcome(folder, name)
        result = saved or run(name, settings, n=n, episode=episode, runs_dir=runs_dir, criterion=criterion,
                              budget=budget, out_dir=out_dir)
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
                      f"beat it — rerun with a larger --n")

    built: dict[Cause, ReplaySettings] = {}
    for position, cause in enumerate(ORACLE_ORDER):
        settings = _oracle(folder, cause, base, untested, lambda c=cause: build(
            c, base, episode=episode, follow_ups=follow_ups, criterion=criterion, symptom=symptom,
            pristine=pristine, env_script=env_script, ask=ask))
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
        name = f"{first.value}+{second.value}"
        result = measured(name, combine(base, {first: built[first], second: built[second]}))
        if beats(result, baseline):
            return finish(Cause.MULTI_CAUSE, f"{name} passed {_rate(result)} against the baseline's {_rate(baseline)}",
                          (first.value, second.value))
    return finish(Cause.MULTI_CAUSE, f"only every change together passed ({_rate(together)})",
                  tuple(c.value for c in built))


def _oracle(folder: Path, cause: Cause, base: ReplaySettings, untested: dict[str, str], make) -> ReplaySettings | None:
    path = folder / VARIANTS_DIR / f"{cause.value}.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        made = make()
        data = {"untested": made.untested_reason} if made.settings is None else settings_to_json(made.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    if "untested" in data:
        untested[cause.value] = data["untested"]
        return None
    return settings_from_json(data)


def _rate(outcome: VariantOutcome) -> str:
    return f"{outcome.passes}/{outcome.runs}"
