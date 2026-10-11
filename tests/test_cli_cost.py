from culprit.replay import REPLAY_BUDGET_FLOOR_USD
import json

import pytest

import culprit.cli as cli
from culprit.diagnose import BatchSummary, VariantOutcome, Verdict
from culprit.model import Criterion, save_criterion
from culprit.store import episode_dir
from records import human, snapshot


def ready(write_session, tmp_path, control=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wrong-result"}')
    if control:
        baseline = episode_dir(runs, "s1:p1") / "replays" / "baseline"
        baseline.mkdir(parents=True)
        (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 10, "runs": 10,
                                                           "cost_usd": 1}))
    return []


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def fake_run_variant(variant, settings, **kwargs):
        calls.append(variant)
        return VariantOutcome(variant, 0, kwargs["n"], 1.0)

    def fake_diagnose(*args, **kwargs):
        calls.append("diagnose")
        return Verdict("s1:p1", "wrong-result", "harness", "e", {}, {})

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    monkeypatch.setattr(cli, "run_selftest", lambda *a, **k: calls.append("selftest") or [])
    monkeypatch.setattr(cli, "diagnose_all", lambda *a, **k: calls.append("diagnose-all") or BatchSummary())
    return calls


@pytest.mark.parametrize("argv", [
    ["diagnose", "s1:p1", "--yes"], ["selftest", "s1:p1", "-y"], ["diagnose", "--dry-run"],
    ["diagnose", "s1:p1", "--budget", "3"], ["diagnose", "s1:p1", "--replay-budget", "3"],
    ["selftest", "s1:p1", "--budget-per-fault", "3"], ["diagnose", "--per-episode", "3"],
    ["diagnose", "--total", "3"]])
def test_spend_is_not_set_or_confirmed_by_flags(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2


@pytest.mark.parametrize("tty", [True, False])
def test_the_plan_is_printed_on_stderr_and_nothing_is_asked(write_session, tmp_path, capsys, monkeypatch, spy, tty):
    ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: tty)

    def no_questions():
        raise AssertionError("asked a question before spending")

    monkeypatch.setattr("builtins.input", no_questions)
    assert cli.main(["selftest", "s1:p1"]) == 1  # replays the missing baseline, which is not a control
    captured = capsys.readouterr()
    assert "up to 15 replays" in captured.err and "cap $3750.00" in captured.err
    assert "per replay: up to $10.00" in captured.err
    assert "up to" not in captured.out
    assert spy == ["baseline"]


def test_diagnose_plan_counts_every_variant(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path)
    assert cli.main(["diagnose", "s1:p1"]) == 0
    # no env-fix: baseline + 4 oracles + combined + 6 pairs + the original model at max effort, 15 attempts each
    # (N = 10 plus 5 void retries)
    err = capsys.readouterr().err
    assert "up to 195 replays and 8 drafting calls" in err
    assert "per replay: up to $10.00 (twice that in harness variants)" in err
    assert spy == ["diagnose"]


def test_selftest_plan_sums_every_planted_fault(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path, control=True)
    assert cli.main(["selftest", "s1:p1"]) == 0
    err = capsys.readouterr().err
    assert "up to 975 replays" in err and "cap $14250.00" in err  # five faults at $2850 each
    assert spy == ["selftest"]


def test_diagnose_queue_plan_covers_only_ready_episodes_within_the_total(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path)
    runs = tmp_path / "runs"
    (runs / "candidates.json").write_text(json.dumps([{"summary": {"episode_id": "s1:p1"}, "score": 3},
                                                      {"summary": {"episode_id": "s2:p1"}, "score": 2}]))
    assert cli.main(["diagnose"]) == 0
    err = capsys.readouterr().err
    assert "up to 195 replays" in err and "at most $2801.50 more" in err and "cap $75000.00" in err
    assert spy == ["diagnose-all"]


def test_the_default_caps_never_stop_a_full_diagnose_at_n():
    from culprit.diagnose import diagnose_cost
    from culprit.store import ORACLE_ORDER, Cause
    assert cli.RUNS_PER_VARIANT == 10
    for replay_cap in (REPLAY_BUDGET_FLOOR_USD, 15.0, 42.0):
        every_oracle = diagnose_cost(replay_cap, ORACLE_ORDER, n=cli.RUNS_PER_VARIANT, spent_usd=0,
                                     cap_usd=float("inf"), drafts=2)
        planted = diagnose_cost(replay_cap, [c for c in ORACLE_ORDER if c is not Cause.ENVIRONMENT],
                                n=cli.RUNS_PER_VARIANT, spent_usd=0, cap_usd=float("inf"), drafts=2)
        assert every_oracle.max_usd <= cli.episode_cap_usd(replay_cap)
        assert planted.max_usd <= cli.fault_cap_usd(replay_cap)
    assert cli.episode_cap_usd(REPLAY_BUDGET_FLOOR_USD) == 3750.0
    assert cli.fault_cap_usd(REPLAY_BUDGET_FLOOR_USD) == 2850.0
    # the batch total covers 20 episodes at the floor's worst case; each episode keeps its own cap within it
    assert cli.TOTAL_CAP_USD == 20 * cli.episode_cap_usd(REPLAY_BUDGET_FLOOR_USD)


def test_an_episode_that_cost_more_gets_a_bigger_per_replay_and_episode_cap(write_session, tmp_path, capsys, spy):
    from records import assistant, cost_state
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo)), assistant(),
                   cost_state(6.0)], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wrong-result"}')
    assert cli.main(["diagnose", "s1:p1"]) == 0
    err = capsys.readouterr().err
    assert "per replay: up to $18.00" in err  # 3 × $6.00
    assert f"cap ${cli.episode_cap_usd(18.0):.2f}" in err
