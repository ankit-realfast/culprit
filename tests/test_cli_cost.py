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
    return ["--projects", str(tmp_path / "projects"), "--runs", str(runs)]


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


def test_dry_run_prints_the_plan_and_runs_nothing(write_session, tmp_path, capsys, spy):
    flags = ready(write_session, tmp_path)
    assert cli.main(["baseline", "s1:p1", *flags, "--n", "5", "--replay-budget", "3", "--budget", "100",
                     "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "up to 10 replays" in out and "at most $35.00 more" in out
    assert spy == []


def test_without_a_terminal_and_without_yes_nothing_runs_and_it_exits_3(write_session, tmp_path, capsys,
                                                                        monkeypatch, spy):
    flags = ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: False)
    assert cli.main(["baseline", "s1:p1", *flags]) == 3
    err = capsys.readouterr().err
    assert "--yes" in err and "up to" in err
    assert spy == []


def test_yes_runs_without_asking(write_session, tmp_path, monkeypatch, spy):
    flags = ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: False)
    assert cli.main(["baseline", "s1:p1", *flags, "--yes"]) == 0
    assert spy == ["baseline"]


@pytest.mark.parametrize("answer, code, ran", [("y", 0, ["baseline"]), ("", 1, []), ("no", 1, [])])
def test_at_a_terminal_it_asks_first(write_session, tmp_path, capsys, monkeypatch, spy, answer, code, ran):
    flags = ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda: asked.append(True) or answer)
    assert cli.main(["baseline", "s1:p1", *flags]) == code
    assert asked == [True] and spy == ran
    assert "proceed? [y/N] " in capsys.readouterr().err  # asked on stderr, so stdout stays clean


def test_dry_run_does_not_retire_runs_judged_against_an_older_criterion(write_session, tmp_path, spy):
    flags = ready(write_session, tmp_path)
    folder = episode_dir(tmp_path / "runs", "s1:p1")
    (folder / "criterion_used.json").write_text('{"key": "older"}')
    (folder / "replays" / "baseline").mkdir(parents=True)
    assert cli.main(["baseline", "s1:p1", *flags, "--dry-run"]) == 0
    assert (folder / "replays").is_dir() and not (folder / "superseded").exists()


def test_diagnose_dry_run_counts_every_variant(write_session, tmp_path, capsys, spy):
    flags = ready(write_session, tmp_path)
    assert cli.main(["diagnose", "s1:p1", *flags, "--dry-run"]) == 0
    out = capsys.readouterr().out
    # no env-fix: baseline + 4 oracles + combined + 6 pairs, 10 attempts each
    assert "up to 120 replays and 8 drafting calls" in out
    assert spy == []


def test_selftest_dry_run_sums_every_planted_fault(write_session, tmp_path, capsys, spy):
    flags = ready(write_session, tmp_path, control=True)
    assert cli.main(["selftest", "s1:p1", *flags, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "up to 600 replays" in out and "cap $200.00" in out
    assert spy == []


def test_diagnose_all_dry_run_covers_only_ready_episodes_within_the_total(write_session, tmp_path, capsys, spy):
    flags = ready(write_session, tmp_path)
    runs = tmp_path / "runs"
    (runs / "candidates.json").write_text(json.dumps([{"summary": {"episode_id": "s1:p1"}, "score": 3},
                                                      {"summary": {"episode_id": "s2:p1"}, "score": 2}]))
    assert cli.main(["diagnose-all", *flags, "--total", "25", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "up to 120 replays" in out and "at most $25.00 more" in out
    assert spy == []
