import json
from dataclasses import replace

from culprit.diagnose import (NOT_RUN_REASON, EpisodeBudget, VariantBuild, VariantOutcome, beats, diagnose,
                              load_verdict, reproducible)
from culprit.logs import episodes_from_session
from culprit.model import Criterion
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
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 4,
                        "original-max-effort": 0, "latest-in-family": 0})
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
    first = scripted_run({"baseline": 0, "task": 0, "environment": 0, "agent-setup": 0, "harness": 0, "model": 5,
                          "original-max-effort": 0, "latest-in-family": 0})
    run_diagnose(write_session, tmp_path, first)
    folder = episode_dir(tmp_path / "runs", "s1:p1")
    for name in ["baseline", "task", "environment", "agent-setup", "harness", "model", "original-max-effort",
                 "latest-in-family"]:
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


def test_when_fable_at_max_effort_wins_the_original_model_at_max_effort_runs_next(write_session, tmp_path):
    from culprit.diagnose import build_variant
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 5,
                        "original-max-effort": 4, "latest-in-family": 0})

    def build(cause, base, **kwargs):
        if cause is Cause.MODEL:
            return build_variant(cause, base, **kwargs)
        return VariantBuild(None, "untested") if cause is Cause.ENVIRONMENT else VariantBuild(base, None)

    verdict = run_diagnose(write_session, tmp_path, run, build=build)
    assert verdict.cause == "model"
    name, settings = run.calls[-2]
    assert name == "original-max-effort"
    assert (settings.model, settings.effort) == ("claude-opus-5", "max")
    assert "raise effort (original model at max effort passed 4/5)" in verdict.evidence
    assert verdict.outcomes["original-max-effort"]["passes"] == 4


def test_when_only_fable_passes_the_verdict_says_it_needs_fable(write_session, tmp_path):
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 5,
                        "original-max-effort": 1, "latest-in-family": 0})
    verdict = run_diagnose(write_session, tmp_path, run, build=build_all_tested(untested=(Cause.ENVIRONMENT,)))
    assert verdict.cause == "model"
    assert ("needs Fable (original model at max effort did not pass: 1/5; the latest opus, claude-opus-5-5, did not "
            "pass: 0/5)") in verdict.evidence


def test_an_episode_on_fable_at_lower_effort_tests_fable_at_max_effort_only(write_session, tmp_path):
    from culprit.diagnose import build_variant
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 5})

    def build(cause, base, **kwargs):
        if cause is Cause.MODEL:
            return build_variant(cause, base, **kwargs)
        return VariantBuild(None, "untested") if cause is Cause.ENVIRONMENT else VariantBuild(base, None)

    fable = replace(BASE, model="claude-fable-5-1", effort="high")
    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, fable, runs_dir=tmp_path / "runs", follow_ups=[],
                       env_script=None, n=5, budget=EpisodeBudget(100.0), run=run, build=build)
    assert verdict.cause == "model"
    assert [name for name, _ in run.calls][-1] == "model"
    assert run.calls[-1][1].effort == "max"
    assert "raise effort (Fable at max effort passed" in verdict.evidence


def test_an_episode_already_at_max_effort_needs_no_effort_step_to_say_it_needs_fable(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    run = scripted_run({"baseline": 0, "task": 0, "agent-setup": 0, "harness": 0, "model": 5, "latest-in-family": 0})
    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, replace(BASE, effort="max"),
                       runs_dir=tmp_path / "runs", follow_ups=[], env_script=None, n=5, budget=EpisodeBudget(100.0),
                       run=run, build=build_all_tested(untested=(Cause.ENVIRONMENT,)))
    assert "original-max-effort" not in [name for name, _ in run.calls]
    assert "needs Fable (the original model already ran at max effort" in verdict.evidence


def model_only_build(cause, base, **kwargs):
    from culprit.diagnose import build_variant
    if cause is Cause.MODEL:
        return build_variant(cause, base, **kwargs)
    return VariantBuild(None, "untested")


def test_diagnose_resolves_the_oracle_alias_and_tests_an_older_fable_against_it(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    run = scripted_run({"baseline": 0, "model": 0})
    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, replace(BASE, model="claude-fable-5", effort="max"),
                       runs_dir=tmp_path / "runs", follow_ups=[], env_script=None, n=5, budget=EpisodeBudget(100.0),
                       run=run, build=model_only_build)
    assert [name for name, _ in run.calls] == ["baseline", "model"]
    assert run.calls[1][1].model == "fable"
    assert "model" not in verdict.untested


def test_an_episode_on_what_the_oracle_means_today_at_max_effort_skips_the_model_oracle(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    run = scripted_run({"baseline": 0})
    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, replace(BASE, model="claude-fable-5-1", effort="max"),
                       runs_dir=tmp_path / "runs", follow_ups=[], env_script=None, n=5, budget=EpisodeBudget(100.0),
                       run=run, build=model_only_build)
    assert [name for name, _ in run.calls] == ["baseline"]
    assert "claude-fable-5-1" in verdict.untested["model"]


def test_a_paid_resolution_is_charged_to_the_episode(write_session, tmp_path, monkeypatch):
    import culprit.model as model
    from culprit.diagnose import stored_spend
    monkeypatch.setattr(model, "resolve_alias", lambda alias: model.Resolution("claude-fable-5-1", 0.05))
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    budget = EpisodeBudget(100.0)
    diagnose(episode, CRITERION, Symptom.WRONG_RESULT, BASE, runs_dir=tmp_path / "runs", follow_ups=[],
             env_script=None, n=5, budget=budget, run=scripted_run({"baseline": 0, "model": 0}),
             build=model_only_build)
    assert budget.spent_usd == 0.05
    assert stored_spend(episode_dir(tmp_path / "runs", "s1:p1")) == 0.05


def model_wins(write_session, tmp_path, base, passes):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    run = scripted_run({"baseline": 0, "model": 5} | passes)
    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, base, runs_dir=tmp_path / "runs", follow_ups=[],
                       env_script=None, n=5, budget=EpisodeBudget(100.0), run=run, build=model_only_build)
    return verdict, run.calls


def test_when_fable_wins_the_latest_of_the_episodes_own_family_runs_at_its_original_effort(write_session, tmp_path):
    verdict, calls = model_wins(write_session, tmp_path, replace(BASE, effort="high"),
                                {"original-max-effort": 1, "latest-in-family": 4})
    assert [name for name, _ in calls] == ["baseline", "model", "original-max-effort", "latest-in-family"]
    assert (calls[-1][1].model, calls[-1][1].effort) == ("opus", "high")
    assert verdict.cause == "model"
    assert verdict.evidence.endswith("— upgrade to the latest opus (claude-opus-5-5 passed 4/5)")


def test_when_both_cheaper_fixes_pass_raising_effort_is_listed_first(write_session, tmp_path):
    verdict, _ = model_wins(write_session, tmp_path, replace(BASE, effort="high"),
                            {"original-max-effort": 4, "latest-in-family": 5})
    assert verdict.evidence.endswith("— raise effort (original model at max effort passed 4/5), or upgrade to the "
                                     "latest opus (claude-opus-5-5 passed 5/5)")


def test_the_upgrade_names_the_model_it_actually_ran_as(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))

    def run(name, settings, **kwargs):
        passes = {"baseline": 0, "model": 5, "original-max-effort": 0, "latest-in-family": 5}[name]
        return VariantOutcome(name, passes, 5, 1.0, models={"claude-opus-6": 5} if name == "latest-in-family" else {})

    verdict = diagnose(episode, CRITERION, Symptom.WRONG_RESULT, BASE, runs_dir=tmp_path / "runs", follow_ups=[],
                       env_script=None, n=5, budget=EpisodeBudget(100.0), run=run, build=model_only_build)
    assert "upgrade to the latest opus (claude-opus-6 passed 5/5)" in verdict.evidence


def test_an_episode_already_at_max_effort_still_tries_its_familys_latest(write_session, tmp_path):
    verdict, calls = model_wins(write_session, tmp_path, replace(BASE, effort="max"), {"latest-in-family": 0})
    assert [name for name, _ in calls] == ["baseline", "model", "latest-in-family"]
    assert calls[-1][1].effort == "max"
    assert ("needs Fable (the original model already ran at max effort: the baseline passed 0/5; the latest opus, "
            "claude-opus-5-5, did not pass: 0/5)") in verdict.evidence


def test_the_family_step_is_skipped_when_it_would_test_nothing_new(write_session, tmp_path):
    for model in ("claude-mythos-1", "claude-opus-5-5", "claude-fable-5", None):
        # an unknown family; the family's latest already; the oracle's own family; no model logged
        verdict, calls = model_wins(write_session, tmp_path / str(model), replace(BASE, model=model, effort="high"),
                                    {"original-max-effort": 0})
        assert "latest-in-family" not in [name for name, _ in calls], model
        assert "needs Fable (original model at max effort did not pass: 0/5)" in verdict.evidence


def test_changing_only_the_limits_counts_as_a_changed_criterion():
    from dataclasses import replace
    from culprit.diagnose import criterion_key
    from culprit.model import Limits
    import hashlib, json
    # a criterion without limits keys as it did before limits existed, so its earlier runs are not retired
    before = json.dumps([CRITERION.success_means, list(CRITERION.checks), list(CRITERION.must_not)])
    assert criterion_key(CRITERION) == hashlib.sha256(before.encode()).hexdigest()[:16]
    assert criterion_key(replace(CRITERION, symptom="wasteful")) == criterion_key(CRITERION)
    assert criterion_key(replace(CRITERION, limits=Limits(max_tool_calls=3))) != criterion_key(CRITERION)
