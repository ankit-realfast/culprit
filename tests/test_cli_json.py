import json

import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import VariantOutcome
from culprit.store import episode_dir
from culprit.verdict import Verdict
from records import assistant, human, interrupt, snapshot


def only_json(capsys):
    out = capsys.readouterr().out
    return json.loads(out)  # fails unless stdout is exactly one JSON document


def ready(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wrong-result"}')
    return []


def test_triage_json(write_session, tmp_path, capsys):
    write_session([human("clean prompt", "p1", session_id="s1"), assistant(),
                   human("went wrong", "p2", session_id="s1"), assistant(), interrupt("p2")])
    runs = tmp_path / "runs"
    assert cli.main(["triage", "--json"]) == 0
    data = only_json(capsys)
    assert data["scanned"] == 2 and data["queued"] == 1
    [row] = data["candidates"]
    assert row["row"] == 1 and row["episode_id"] == "s1:p2" and row["short_id"] == "s1:p2"
    assert row["signals"]["interrupted"] is True and row["prompt_excerpt"] == "went wrong"


def test_status_json_for_the_queue_and_for_one_episode(write_session, tmp_path, capsys):
    ready(write_session, tmp_path)
    assert cli.main(["status", "--json"]) == 0
    [episode] = only_json(capsys)["episodes"]
    assert episode["episode_id"] == "s1:p1" and episode["stage"] == "restored"
    assert episode["next_step"] == "run `culprit baseline s1:p1`"
    assert cli.main(["status", "s1:p1", "--json"]) == 0
    one = only_json(capsys)
    assert one["criterion"]["approved"] is True and one["symptom"] == "wrong-result" and one["verdict"] is None


def test_baseline_json(write_session, tmp_path, capsys, monkeypatch):
    flags = ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "run_variant", lambda variant, settings, **k: VariantOutcome(variant, 1, k["n"], 3.1))
    assert cli.main(["baseline", "s1:p1", *flags, "--yes", "--json"]) == 0
    data = only_json(capsys)
    assert data["outcome"] == {"variant": "baseline", "passes": 1, "runs": 5, "cost_usd": 3.1, "void": 0}
    assert data["episode_id"] == "s1:p1" and "next" in data


def test_a_dry_run_with_json_prints_the_plan_as_json(write_session, tmp_path, capsys):
    flags = ready(write_session, tmp_path)
    assert cli.main(["baseline", "s1:p1", *flags, "--dry-run", "--json"]) == 0
    assert only_json(capsys)["plan"]["replays"] == 10


def test_diagnose_json(write_session, tmp_path, capsys, monkeypatch):
    flags = ready(write_session, tmp_path)
    monkeypatch.setattr(cli, "diagnose", lambda *a, **k: Verdict("s1:p1", "wrong-result", "harness", "4/5", {},
                                                                  {"environment": "no fix"}))
    assert cli.main(["diagnose", "s1:p1", *flags, "--yes", "--json"]) == 0
    data = only_json(capsys)
    assert data["verdict"]["cause"] == "harness" and data["verdict"]["untested"] == {"environment": "no fix"}
    assert data["judge"]["calibrated"] is False and data["selftest"] == {"correct": 0, "total": 0}


def test_calibration_json(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    assert cli.main(["calibration", "--json"]) == 0
    data = only_json(capsys)
    assert data == {"labelled": 0, "agreed": 0, "kappa": None, "calibrated": False, "min_labels": 20,
                    "min_kappa": 0.6}
