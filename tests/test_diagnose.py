import json
from dataclasses import replace

from culprit.model import Criterion
from culprit.diagnose import (NOT_RUN_REASON, EpisodeBudget, VariantBuild, VariantOutcome, beats, diagnose,
                              load_verdict, reproducible)
from culprit.logs import episodes_from_session
from culprit.replay import ReplaySettings
from culprit.store import Cause, Symptom, episode_dir
from records import human

CRITERION = Criterion("works", (), (), approved=True)
BASE = ReplaySettings(prompt="fix", model="claude-opus-5", permission_mode="auto")


def outcome(name, passes, runs=5):
    return VariantOutcome(name, passes, runs, 1.0)


def scripted_run(passes_by_name):
    calls = []

    def run(name, settings, **kwargs):
        calls.append((name, settings))
        return outcome(name, passes_by_name[name])

    run.calls = calls
    return run


def build_all_tested(untested=()):
    def build(cause, base, **kwargs):
        if cause in untested:
            return VariantBuild(None, f"{cause.value} untested")
        return VariantBuild(replace(base, prompt=f"{cause.value} prompt") if cause is Cause.TASK else
                            replace(base, model=f"{cause.value}-model"), None)
    return build


def run_diagnose(write_session, tmp_path, run, build=None, symptom=Symptom.WRONG_RESULT):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    return diagnose(episode, CRITERION, symptom, BASE, runs_dir=tmp_path / "runs", follow_ups=[], env_script=None,
                    n=5, budget=EpisodeBudget(100.0), run=run, build=build or build_all_tested())


def test_beats_and_reproducible_follow_the_spec():
    assert beats(outcome("task", 4), outcome("baseline", 0)) is True
    assert beats(outcome("task", 3), outcome("baseline", 0)) is False
    assert reproducible(outcome("baseline", 2)) is True
    assert reproducible(outcome("baseline", 3)) is False


def test_a_rejected_candidate_is_misjudged_without_replays(write_session, tmp_path):
    run = scripted_run({})
    verdict = run_diagnose(write_session, tmp_path, run, symptom=Symptom.NO_FAILURE)
    assert verdict.cause == "misjudged"
    assert run.calls == []


def test_a_baseline_that_passes_is_not_reproducible_and_runs_no_oracle(write_session, tmp_path):
    run = scripted_run({"baseline": 3})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "not-reproducible"
    assert [name for name, _ in run.calls] == ["baseline"]


def test_the_first_cause_that_beats_the_baseline_wins_and_later_ones_are_not_run(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 1, "environment": 0, "agent-setup": 5})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "agent-setup"
    assert [name for name, _ in run.calls] == ["baseline", "task", "environment", "agent-setup"]
    assert verdict.untested == {"harness": NOT_RUN_REASON, "model": NOT_RUN_REASON}
    assert "5/5" in verdict.evidence and "0/5" in verdict.evidence
    assert load_verdict(episode_dir(tmp_path / "runs", "s1:p1")) == verdict


def test_untested_causes_are_skipped_and_recorded(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 4})
    verdict = run_diagnose(write_session, tmp_path, run, build=build_all_tested(untested=(Cause.ENVIRONMENT,)))
    assert verdict.cause == "model"
    assert verdict.untested["environment"] == "environment untested"


def test_nothing_alone_but_a_pair_is_multi_cause(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 0,
                        "combined": 5, "task+environment": 0, "task+agent-setup": 4})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert (verdict.cause, verdict.pair) == ("multi-cause", ("task", "agent-setup"))


def test_combined_passes_but_no_pair_is_multi_cause_with_the_full_set(write_session, tmp_path):
    singles = {name: 0 for name in ["task", "environment", "agent-setup", "harness", "model"]}
    run = scripted_run({"baseline": 0, "combined": 5} | singles | {name: 0 for name in [
        "task+environment", "task+agent-setup", "task+harness", "task+model", "environment+agent-setup",
        "environment+harness", "environment+model", "agent-setup+harness", "agent-setup+model", "harness+model"]})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "multi-cause"
    assert verdict.pair == ("task", "environment", "agent-setup", "harness", "model")


def test_nothing_passes_even_combined_is_unexplained(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 0,
                        "combined": 1})
    assert run_diagnose(write_session, tmp_path, run).cause == "unexplained"


def test_one_tested_oracle_that_fails_is_unexplained(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "harness": 0})
    only_harness = build_all_tested(untested=(Cause.TASK, Cause.ENVIRONMENT, Cause.AGENT_SETUP, Cause.MODEL))
    verdict = run_diagnose(write_session, tmp_path, run, build=only_harness)
    assert verdict.cause == "unexplained"
    assert [name for name, _ in run.calls] == ["baseline", "harness"]


def test_a_rerun_reuses_saved_variants_and_outcomes(write_session, tmp_path):
    first = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 5})
    run_diagnose(write_session, tmp_path, first)
    folder = episode_dir(tmp_path / "runs", "s1:p1")
    for name in ["baseline", "task", "environment", "agent-setup", "harness", "model"]:
        (folder / "replays" / name).mkdir(parents=True, exist_ok=True)
        (folder / "replays" / name / "outcome.json").write_text(
            json.dumps({"variant": name, "passes": 5 if name == "model" else 0, "runs": 5, "cost_usd": 1.0}))
    builds = []

    def counting_build(cause, base, **kwargs):
        builds.append(cause)
        return build_all_tested()(cause, base)

    second = scripted_run({})
    verdict = run_diagnose(write_session, tmp_path, second, build=counting_build)
    assert verdict.cause == "model"
    assert second.calls == [] and builds == []


def test_a_baseline_no_oracle_could_beat_spends_nothing_more(write_session, tmp_path):
    run = scripted_run({"baseline": 2})
    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "unexplained"
    assert "not testable" in verdict.evidence and "--n" not in verdict.evidence
    assert [name for name, _ in run.calls] == ["baseline"]


def test_a_baseline_with_no_counted_runs_is_not_testable(write_session, tmp_path):
    def run(name, settings, **kwargs):
        return VariantOutcome(name, 0, 0, 1.0, void=5)

    verdict = run_diagnose(write_session, tmp_path, run)
    assert verdict.cause == "unexplained" and "not testable" in verdict.evidence


def test_drafting_calls_are_charged_to_the_episode_budget(write_session, tmp_path):
    from culprit.diagnose import stored_spend

    def drafting_build(cause, base, **kwargs):
        kwargs["ask"]("draft", {})
        return VariantBuild(None, "untested")

    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    budget = EpisodeBudget(100.0)
    diagnose(episode, CRITERION, Symptom.WRONG_RESULT, BASE, runs_dir=tmp_path / "runs", follow_ups=[],
             env_script=None, n=5, budget=budget, run=scripted_run({"baseline": 0}), build=drafting_build,
             ask=lambda prompt, schema: {})
    assert budget.spent_usd == 5 * 0.5
    assert stored_spend(episode_dir(tmp_path / "runs", "s1:p1")) == 5 * 0.5


def test_runs_judged_against_an_older_criterion_are_set_aside_but_still_counted(write_session, tmp_path):
    from culprit.diagnose import retire_if_criterion_changed
    from culprit.diagnose import stored_spend
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    folder = episode_dir(tmp_path / "runs", "s1:p1")
    old_criterion = Criterion("the old definition", (), (), approved=True)
    assert retire_if_criterion_changed(folder, old_criterion) is False
    old_run = folder / "replays" / "baseline" / "0"
    old_run.mkdir(parents=True)
    (old_run / "result.json").write_text(json.dumps({"cost_usd": 2.0}))
    (folder / "replays" / "baseline" / "outcome.json").write_text(
        json.dumps({"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 2.0}))
    run = scripted_run({"baseline": 3})
    verdict = run_diagnose(write_session, tmp_path, run)  # CRITERION differs from old_criterion
    assert [name for name, _ in run.calls] == ["baseline"]  # not reused
    assert verdict.cause == "not-reproducible"
    assert (folder / "superseded" / "1" / "replays" / "baseline" / "0" / "result.json").exists()
    assert stored_spend(folder) == 2.0


def test_progress_reaches_every_variant_run(write_session, tmp_path):
    seen = []

    def run(name, settings, **kwargs):
        seen.append((name, kwargs["progress"]))
        return outcome(name, {"baseline": 0}.get(name, 5))

    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    listener = []
    diagnose(episode, CRITERION, Symptom.WRONG_RESULT, BASE, runs_dir=tmp_path / "runs", follow_ups=[],
             env_script=None, n=5, budget=EpisodeBudget(100.0), run=run, build=build_all_tested(),
             progress=listener.append)
    assert [name for name, _ in seen] == ["baseline", "task"]
    assert all(p == listener.append for _, p in seen)
