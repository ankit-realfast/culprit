import json

import pytest

import culprit.cli as cli
from culprit.model import Criterion, save_criterion
from culprit.store import episode_dir
from culprit.trust import SelftestResult
from records import human, snapshot


def judged(runs, i, passed):
    run_dir = runs / "s1__p1" / "replays" / "baseline" / str(i)
    run_dir.mkdir(parents=True)
    (run_dir / "judgement.json").write_text(json.dumps({"passed": passed, "reason": "r", "by": "model"}))
    (run_dir / "result.json").write_text(json.dumps({"result_text": "done"}))
    (run_dir / "diff.patch").write_text("")
    return run_dir


def answering(monkeypatch, *answers):
    """A terminal on stdin that types these answers, one per question; asking past them fails the test."""
    queue = list(answers)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))
    return queue


def test_review_labels_each_judged_replay_in_turn_then_status_shows_the_agreement(tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    judged(runs, 0, True)
    judged(runs, 1, False)
    left = answering(monkeypatch, "p", "f")
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert "Replay · s1:p1 · baseline run 0\n" in out and "Replay · s1:p1 · baseline run 1\n" in out
    assert "  The judge said: pass" in out
    assert "✓ Every judged replay is labelled" in out
    assert out.endswith("Next\n  1. Label more replays as diagnoses add them:  culprit review\n")
    assert left == []
    assert json.loads((runs / "s1__p1/replays/baseline/0/human_label.json").read_text())["passed"] is True
    assert json.loads((runs / "s1__p1/replays/baseline/1/human_label.json").read_text())["passed"] is False
    assert cli.main(["status"]) == 0
    assert "Judge: 2 of 2 labels agree" in capsys.readouterr().out


def test_review_skips_and_quits_on_request(tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    judged(runs, 0, True)
    judged(runs, 1, True)
    judged(runs, 2, True)
    answering(monkeypatch, "s", "maybe", "pass", "q")
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert not (runs / "s1__p1/replays/baseline/0/human_label.json").exists()  # skipped
    assert (runs / "s1__p1/replays/baseline/1/human_label.json").exists()  # "maybe" asked again, then "pass"
    assert not (runs / "s1__p1/replays/baseline/2/human_label.json").exists()  # quit before it
    assert "Judge: 1 of 1 labels agree" in out


def test_review_without_a_terminal_is_not_ready(tmp_path, capsys, monkeypatch):
    judged(tmp_path / "runs", 0, True)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: False)
    assert cli.main(["review"]) == 3
    assert "terminal" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["label", "s1__p1/replays/baseline/0", "pass"], ["calibration"]])
def test_label_and_calibration_are_not_commands(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2


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
    outcome.write_text('{"variant": "baseline", "passes": 7, "runs": 10, "cost_usd": 1.0}')
    assert cli.main(["selftest", "s1:p1"]) == 1
    err = capsys.readouterr().err
    assert "episode s1:p1 is not a control" in err and "passed 7 of 10" in err and "at least 8 of 10" in err
    assert "Next\n  1. " in err


def test_selftest_prints_each_fault_and_the_accuracy(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    baseline = episode_dir(runs, "s1:p1") / "replays" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 10, "runs": 10, "cost_usd": 1}))
    monkeypatch.setattr(cli, "run_selftest", lambda *a, **k: [SelftestResult("weak-model", "model", "model"),
                                                              SelftestResult("truncated-prompt", "task", "harness")])
    assert cli.main(["selftest", "s1:p1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Episode s1:p1\n")
    assert "Planted faults\n  1. weak-model: expected model, got model — named correctly\n" in out
    assert "  2. truncated-prompt: expected task, got harness — missed\n" in out
    assert "Score\n  1 of 2 planted faults named correctly\n" in out
    assert out.endswith("Next\n  1. See how far to trust the verdicts:  culprit status\n")


def test_review_asks_with_the_shared_chooser(tmp_path, capsys, monkeypatch):
    judged(tmp_path / "runs", 0, True)
    asked = []
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    answers = ["2"]
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or answers.pop(0))
    assert cli.main(["review"]) == 0
    assert asked == ["\nYour verdict? 1 pass · 2 fail · 3 skip · 4 quit: "]
    assert json.loads((tmp_path / "runs/s1__p1/replays/baseline/0/human_label.json").read_text())["passed"] is False
