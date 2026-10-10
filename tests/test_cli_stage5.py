import json

import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.selftest import SelftestResult
from culprit.store import episode_dir
from records import human, snapshot


def judged(runs, i, passed):
    run_dir = runs / "s1__p1" / "replays" / "baseline" / str(i)
    run_dir.mkdir(parents=True)
    (run_dir / "judgement.json").write_text(json.dumps({"passed": passed, "reason": "r", "by": "model"}))
    (run_dir / "result.json").write_text(json.dumps({"result_text": "done"}))
    (run_dir / "diff.patch").write_text("")
    return run_dir


def test_review_then_label_then_calibration(tmp_path, capsys):
    runs = tmp_path / "runs"
    judged(runs, 0, True)
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert "s1__p1/replays/baseline/0" in out and "judge: pass" in out
    assert cli.main(["label", "s1__p1/replays/baseline/0", "pass"]) == 0
    assert cli.main(["review"]) == 0
    assert "every judged replay is labelled" in capsys.readouterr().out
    assert cli.main(["calibration"]) == 0
    out = capsys.readouterr().out
    assert "1 labelled" in out and "uncalibrated" in out


def test_trust_lines_report_calibration_and_selftest(tmp_path):
    lines = cli.trust_lines(tmp_path / "runs")
    assert any("uncalibrated" in line for line in lines)
    assert any("selftest: not run" in line for line in lines)


def test_selftest_refuses_an_episode_that_is_not_a_control(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    def must_not_run(*args, **kwargs):
        raise AssertionError("selftest spent money on an episode that is not a control")

    monkeypatch.setattr(cli, "run_selftest", must_not_run)
    outcome = episode_dir(runs, "s1:p1") / "replays" / "baseline" / "outcome.json"
    outcome.parent.mkdir(parents=True)
    outcome.write_text('{"variant": "baseline", "passes": 1, "runs": 5, "cost_usd": 1.0}')
    assert cli.main(["selftest", "s1:p1"]) == 1
    err = capsys.readouterr().err
    assert "control" in err and "1 of 5" in err


def test_selftest_prints_each_fault_and_the_accuracy(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    baseline = episode_dir(runs, "s1:p1") / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 5, "runs": 5, "cost_usd": 1}))
    monkeypatch.setattr(cli, "run_selftest", lambda *a, **k: [SelftestResult("weak-model", "model", "model"),
                                                              SelftestResult("truncated-prompt", "task", "harness")])
    assert cli.main(["selftest", "s1:p1"]) == 0
    out = capsys.readouterr().out
    assert "weak-model: expected model, got model ✓" in out
    assert "truncated-prompt: expected task, got harness ✗" in out
    assert "1 of 2 planted faults named correctly" in out
