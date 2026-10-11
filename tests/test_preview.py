import json

from culprit.diagnose import CRITERION_USED_FILE, CostPlan, criterion_key, diagnose_cost, diagnose_cost_for, plan_cost
from culprit.model import Criterion
from culprit.store import ORACLE_ORDER, Cause

CRITERION = Criterion("works", (), (), approved=True)


def test_each_variant_counts_n_runs_plus_every_void_retry_and_its_judge():
    plan = plan_cost([3.0], n=5, spent_usd=0.0, cap_usd=100.0)
    assert plan.replays == 10  # 5 counted runs + 5 void retries
    assert plan.max_usd == 35.0  # 10 × ($3 replay + $0.50 judge)


def test_the_most_it_can_spend_is_what_is_left_of_the_cap():
    plan = plan_cost([3.0], n=5, spent_usd=4.0, cap_usd=10.0)
    assert plan.max_usd == 6.0 and plan.spent_usd == 4.0
    assert plan_cost([3.0], n=5, spent_usd=12.0, cap_usd=10.0).max_usd == 0.0


def test_diagnose_counts_baseline_every_oracle_together_and_every_pair():
    plan = diagnose_cost(1.0, ORACLE_ORDER, n=5, spent_usd=0.0, cap_usd=10_000.0, drafts=2)
    assert plan.replays == (1 + 5 + 1 + 10) * 10
    assert plan.calls == 2 * 2 * 2  # two drafted oracles, two attempts, draft + leak check each


def test_the_harness_oracle_and_every_mix_with_it_run_at_twice_the_replay_cap():
    alone = diagnose_cost(1.0, [Cause.MODEL], n=1, spent_usd=0.0, cap_usd=10_000.0)
    harness = diagnose_cost(1.0, [Cause.HARNESS], n=1, spent_usd=0.0, cap_usd=10_000.0)
    assert harness.max_usd - alone.max_usd == 1.0 * 6  # one extra replay-cap's worth per attempt


def test_one_oracle_means_no_combined_or_pair_runs_and_stored_outcomes_are_free():
    assert diagnose_cost(1.0, [Cause.MODEL], n=5, spent_usd=0.0, cap_usd=1e4).replays == 20
    assert diagnose_cost(1.0, [Cause.MODEL], n=5, spent_usd=0.0, cap_usd=1e4, done=frozenset({"baseline"})).replays == 10


def test_plans_add_up_and_a_shared_total_caps_them():
    a = CostPlan(10, 0, 30.0, 1.0, 40.0)
    total = a + CostPlan(20, 4, 50.0, 2.0, 40.0)
    assert (total.replays, total.calls, total.max_usd, total.spent_usd) == (30, 4, 80.0, 3.0)
    assert total.within(25.0).max_usd == 25.0


def test_on_disk_missing_env_fix_and_a_fable_episode_drop_their_oracles(tmp_path):
    plan = diagnose_cost_for(tmp_path, CRITERION, 1.0, model="claude-fable-5-1", effort="max", env_fix=False, n=5,
                             spent_usd=0.0, cap_usd=1e4, oracle_id="claude-fable-5-1")
    # task, agent-setup, harness: baseline + 3 + combined + 3 pairs
    assert plan.replays == 8 * 10 and plan.calls == 8
    older = diagnose_cost_for(tmp_path, CRITERION, 1.0, model="claude-fable-5", effort="max", env_fix=False, n=5,
                              spent_usd=0.0, cap_usd=1e4, oracle_id="claude-fable-5-1")
    assert older.replays > plan.replays  # an older Fable still tests the model oracle


def test_on_disk_reuses_drafted_variants_stored_outcomes_and_skips_untested_ones(tmp_path):
    (tmp_path / CRITERION_USED_FILE).write_text(json.dumps({"key": criterion_key(CRITERION)}))
    variants = tmp_path / "variants"
    variants.mkdir()
    (variants / "task.json").write_text(json.dumps({"untested": "the leak check rejected 2 drafts"}))
    (variants / "agent-setup.json").write_text(json.dumps({"prompt": "x"}))
    baseline = tmp_path / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1}))
    plan = diagnose_cost_for(tmp_path, CRITERION, 1.0, model=None, env_fix=False, n=5, spent_usd=0.0, cap_usd=1e4)
    # agent-setup, harness, model: 3 + combined + 3 pairs + the original model at max effort, baseline already
    # stored, no drafting left
    assert plan.replays == 8 * 10 and plan.calls == 0


def test_on_disk_a_changed_criterion_reuses_nothing(tmp_path):
    (tmp_path / CRITERION_USED_FILE).write_text(json.dumps({"key": "something-else"}))
    baseline = tmp_path / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1}))
    plan = diagnose_cost_for(tmp_path, CRITERION, 1.0, model=None, env_fix=True, n=5, spent_usd=0.0, cap_usd=1e4)
    assert plan.replays == 18 * 10  # baseline, 5 oracles, combined, 10 pairs, the original model at max effort


def test_the_preview_counts_every_call_a_drafted_oracle_can_make(write_session, tmp_path):
    """The preview's drafting calls per drafted oracle match what building one costs at worst (every draft
    leaks), whatever context the drafter is shown."""
    from culprit.diagnose import CALLS_PER_DRAFT_ATTEMPT, DRAFTED_CAUSES, MAX_DRAFT_ATTEMPTS, build_variant
    from culprit.logs import episodes_from_session
    from culprit.model import Criterion
    from culprit.replay import ReplaySettings
    from culprit.store import Symptom
    from records import assistant, human

    (pristine := tmp_path / "pristine").mkdir()
    (pristine / "CLAUDE.md").write_text("Run the tests.\n")
    episode = list(episodes_from_session(write_session([human("start", "p1", cwd=str(tmp_path / "repo")),
                                                        assistant(text="ok"),
                                                        human("now finish", "p2", cwd=str(tmp_path / "repo"))])))[1]
    for cause in DRAFTED_CAUSES:
        calls = []

        def ask(prompt, schema):
            calls.append(prompt)
            return {"leaks": True, "evidence": "x"} if "leaks" in schema.get("properties", {}) else {"text": "t"}

        build_variant(cause, ReplaySettings(prompt="now finish", model="m", permission_mode="auto",
                                            max_budget_usd=1.0),
                      episode=episode, follow_ups=[], criterion=Criterion("done", (), (), approved=True),
                      symptom=Symptom.STOPPED_SHORT, pristine=pristine, env_script=None, ask=ask)
        assert len(calls) == MAX_DRAFT_ATTEMPTS * CALLS_PER_DRAFT_ATTEMPT


def test_the_model_oracle_adds_the_original_model_at_max_effort_unless_the_episode_ran_on_fable(tmp_path):
    with_step = diagnose_cost(1.0, [Cause.MODEL], n=5, spent_usd=0.0, cap_usd=1e4, effort_step=True)
    assert with_step.replays == 30  # baseline, Fable at max effort, the original model at max effort
    on_fable = diagnose_cost_for(tmp_path, CRITERION, 1.0, model="claude-fable-5-1", effort="high", env_fix=False,
                                 n=5, spent_usd=0.0, cap_usd=1e4, oracle_id="claude-fable-5-1")
    on_opus = diagnose_cost_for(tmp_path, CRITERION, 1.0, model="claude-opus-5", effort="high", env_fix=False,
                                n=5, spent_usd=0.0, cap_usd=1e4)
    # opus: baseline + 4 oracles + combined + 6 pairs + the effort step; fable at high: the same minus the step
    assert on_opus.replays == 13 * 10 and on_fable.replays == 12 * 10
