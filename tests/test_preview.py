import json

from culprit.model import Criterion
from culprit.diagnose import CRITERION_USED_FILE, CostPlan, criterion_key, diagnose_cost, diagnose_cost_for, plan_cost
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
    plan = diagnose_cost_for(tmp_path, CRITERION, 1.0, model="claude-fable-5-1", env_fix=False, n=5,
                             spent_usd=0.0, cap_usd=1e4)
    # task, agent-setup, harness: baseline + 3 + combined + 3 pairs
    assert plan.replays == 8 * 10 and plan.calls == 8


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
    # agent-setup, harness, model: 3 + combined + 3 pairs, baseline already stored, no drafting left
    assert plan.replays == 7 * 10 and plan.calls == 0


def test_on_disk_a_changed_criterion_reuses_nothing(tmp_path):
    (tmp_path / CRITERION_USED_FILE).write_text(json.dumps({"key": "something-else"}))
    baseline = tmp_path / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1}))
    plan = diagnose_cost_for(tmp_path, CRITERION, 1.0, model=None, env_fix=True, n=5, spent_usd=0.0, cap_usd=1e4)
    assert plan.replays == 17 * 10
