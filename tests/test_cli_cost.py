import json

import pytest

import culprit.cli as cli
from culprit.batch import BatchSummary
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import VariantOutcome
from culprit.store import episode_dir
from culprit.verdict import Verdict
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
        (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 5, "runs": 5,
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
    ["baseline", "s1:p1", "--yes"], ["baseline", "s1:p1", "-y"], ["baseline", "s1:p1", "--dry-run"],
    ["baseline", "s1:p1", "--budget", "3"], ["diagnose", "s1:p1", "--replay-budget", "3"],
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
    assert cli.main(["baseline", "s1:p1"]) == 0
    captured = capsys.readouterr()
    assert "up to 10 replays" in captured.err and "cap $40.00" in captured.err
    assert "up to" not in captured.out
    assert spy == ["baseline"]


def test_diagnose_plan_counts_every_variant(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path)
    assert cli.main(["diagnose", "s1:p1"]) == 0
    # no env-fix: baseline + 4 oracles + combined + 6 pairs, 10 attempts each
    assert "up to 120 replays and 8 drafting calls" in capsys.readouterr().err
    assert spy == ["diagnose"]


def test_selftest_plan_sums_every_planted_fault(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path, control=True)
    assert cli.main(["selftest", "s1:p1"]) == 0
    err = capsys.readouterr().err
    assert "up to 600 replays" in err and "cap $200.00" in err
    assert spy == ["selftest"]


def test_diagnose_queue_plan_covers_only_ready_episodes_within_the_total(write_session, tmp_path, capsys, spy):
    ready(write_session, tmp_path)
    runs = tmp_path / "runs"
    (runs / "candidates.json").write_text(json.dumps([{"summary": {"episode_id": "s1:p1"}, "score": 3},
                                                      {"summary": {"episode_id": "s2:p1"}, "score": 2}]))
    assert cli.main(["diagnose"]) == 0
    err = capsys.readouterr().err
    assert "up to 120 replays" in err and "at most $40.00 more" in err and "cap $400.00" in err
    assert spy == ["diagnose-all"]
