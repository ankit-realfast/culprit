import json

import pytest

from culprit.calibration import (Agreement, UnknownRun, agreement, cohens_kappa, judged_runs, next_unlabelled,
                                 record_label, run_id, run_summary)


def judged(runs, episode, variant, i, passed, reason="r"):
    run_dir = runs / episode / "replays" / variant / str(i)
    run_dir.mkdir(parents=True)
    (run_dir / "judgement.json").write_text(json.dumps({"passed": passed, "reason": reason, "by": "model"}))
    (run_dir / "result.json").write_text(json.dumps({"result_text": "I fixed it", "cost_usd": 0.2}))
    (run_dir / "diff.patch").write_text("--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n")
    return run_dir


def test_kappa_matches_the_textbook_formula():
    pairs = [(True, True), (True, True), (False, False), (True, False)]
    assert cohens_kappa(pairs) == pytest.approx(0.5)


def test_kappa_is_undefined_when_one_value_is_certain():
    assert cohens_kappa([(True, True), (True, True)]) is None
    assert cohens_kappa([]) is None


def test_only_real_episode_runs_count_and_selftest_runs_do_not(tmp_path):
    runs = tmp_path / "runs"
    real = judged(runs, "s1__p1", "baseline", 0, True)
    judged(runs / "selftest" / "s1__p1", "weak-model", "baseline", 0, True)
    assert judged_runs(runs) == [real]
    assert run_id(runs, real) == "s1__p1/replays/baseline/0"


def test_labels_drive_agreement_and_the_calibration_gate(tmp_path):
    runs = tmp_path / "runs"
    for i in range(10):
        judged(runs, "s1__p1", "baseline", i, passed=i < 5)
        judged(runs, "s2__p1", "task", i, passed=i < 5)
    for run_dir in judged_runs(runs):
        judge_passed = json.loads((run_dir / "judgement.json").read_text())["passed"]
        record_label(runs, run_id(runs, run_dir), judge_passed)
    result = agreement(runs)
    assert result == Agreement(labelled=20, agreed=20, kappa=1.0)
    assert result.calibrated is True
    assert Agreement(labelled=19, agreed=19, kappa=1.0).calibrated is False
    assert Agreement(labelled=30, agreed=20, kappa=0.4).calibrated is False
    assert Agreement(labelled=30, agreed=30, kappa=None).calibrated is False


def test_labelling_an_unknown_run_is_refused(tmp_path):
    with pytest.raises(UnknownRun, match="s9__p9/replays/baseline/0"):
        record_label(tmp_path, "s9__p9/replays/baseline/0", True)
    assert not (tmp_path / "s9__p9").exists()


def test_next_unlabelled_walks_the_runs_in_order(tmp_path):
    runs = tmp_path / "runs"
    first = judged(runs, "s1__p1", "baseline", 0, True)
    second = judged(runs, "s1__p1", "baseline", 1, False)
    assert next_unlabelled(runs) == first
    record_label(runs, run_id(runs, first), True)
    assert next_unlabelled(runs) == second
    record_label(runs, run_id(runs, second), False)
    assert next_unlabelled(runs) is None


def test_next_unlabelled_passes_over_skipped_runs(tmp_path):
    runs = tmp_path / "runs"
    first = judged(runs, "s1__p1", "baseline", 0, True)
    second = judged(runs, "s1__p1", "baseline", 1, False)
    assert next_unlabelled(runs, skip={first}) == second
    assert next_unlabelled(runs, skip={first, second}) is None


def test_the_summary_shows_answer_judgement_and_diff(tmp_path):
    summary = run_summary(judged(tmp_path, "s1__p1", "baseline", 0, False, reason="tests still fail"))
    for expected in ["I fixed it", "judge: fail — tests still fail", "+new", "1 file changed"]:
        assert expected in summary
