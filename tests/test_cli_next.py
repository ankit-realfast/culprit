import json

import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import VariantOutcome
from culprit.store import episode_dir
from culprit.verdict import Verdict
from records import human, snapshot


def queued(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo)),
                   human("still wrong", "p2", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "candidates.json").write_text(json.dumps([{"score": 5, "summary": {"episode_id": "s1:p1"}}]))
    return tmp_path / "projects", runs


def last_err_line(capsys):
    return capsys.readouterr().err.strip().splitlines()[-1]


def test_confirm_names_the_criterion_draft_next(write_session, tmp_path, capsys):
    _, runs = queued(write_session, tmp_path)
    assert cli.main(["confirm", "s1:p1", "wrong-result", "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit criterion draft s1:p1` then `culprit criterion approve s1:p1`"


def test_criterion_approve_names_restore_next(write_session, tmp_path, capsys):
    _, runs = queued(write_session, tmp_path)
    cli.main(["confirm", "s1:p1", "wrong-result", "--runs", str(runs)])
    save_criterion(runs, "s1:p1", Criterion("works", (), ()))
    capsys.readouterr()
    assert cli.main(["criterion", "approve", "s1:p1", "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit restore s1:p1`"


def test_baseline_names_diagnose_next(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = queued(write_session, tmp_path)
    cli.main(["confirm", "s1:p1", "wrong-result", "--runs", str(runs)])
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir()

    def fake_run_variant(variant, settings, **kwargs):
        folder = episode_dir(runs, "s1:p1") / "replays" / variant
        folder.mkdir(parents=True)
        (folder / "outcome.json").write_text(json.dumps({"variant": variant, "passes": 0, "runs": 5,
                                                         "cost_usd": 1.0, "void": 0}))
        return VariantOutcome(variant, 0, 5, 1.0)

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    capsys.readouterr()
    assert cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit diagnose s1:p1`"


def test_diagnose_and_env_fix_name_their_next_step(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = queued(write_session, tmp_path)
    cli.main(["confirm", "s1:p1", "wrong-result", "--runs", str(runs)])
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir()
    script = tmp_path / "fix.sh"
    script.write_text("true\n")
    capsys.readouterr()
    assert cli.main(["env-fix", "s1:p1", str(script), "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit baseline s1:p1`"

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        verdict = Verdict("s1:p1", "wrong-result", "model", "model passed 4/5", {}, {})
        from culprit.verdict import save_verdict
        save_verdict(episode_dir(runs, "s1:p1"), verdict)
        return verdict

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    assert cli.main(["diagnose", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit report`"


def test_label_names_review_while_runs_are_unlabelled_then_calibration(tmp_path, capsys):
    runs = tmp_path / "runs"
    for i in range(2):
        run_dir = runs / "s1__p1" / "replays" / "baseline" / str(i)
        run_dir.mkdir(parents=True)
        (run_dir / "judgement.json").write_text(json.dumps({"passed": True, "reason": "r", "by": "model"}))
    assert cli.main(["label", "s1__p1/replays/baseline/0", "pass", "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit review`"
    assert cli.main(["label", "s1__p1/replays/baseline/1", "pass", "--runs", str(runs)]) == 0
    assert last_err_line(capsys) == "next: run `culprit calibration`"
