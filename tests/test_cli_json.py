import json

import pytest

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


@pytest.mark.parametrize("argv", [["triage", "--json"], ["diagnose", "--json"], ["diagnose", "s1:p1", "--json"]])
def test_only_status_takes_json(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2


def test_status_json_for_the_queue_and_for_one_episode(write_session, tmp_path, capsys):
    ready(write_session, tmp_path)
    assert cli.main(["status", "--json"]) == 0
    [episode] = only_json(capsys)["episodes"]
    assert episode["episode_id"] == "s1:p1" and episode["stage"] == "restored"
    assert episode["next_step"] == "run `culprit diagnose s1:p1`"
    assert cli.main(["status", "s1:p1", "--json"]) == 0
    one = only_json(capsys)
    assert one["criterion"]["approved"] is True and one["symptom"] == "wrong-result" and one["verdict"] is None


def test_status_json_carries_the_judge_agreement_and_selftest_accuracy(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    assert cli.main(["status", "--json"]) == 0
    data = only_json(capsys)
    assert data["judge"] == {"labelled": 0, "agreed": 0, "kappa": None, "calibrated": False, "min_labels": 20,
                             "min_kappa": 0.6}
    assert data["selftest"] == {"correct": 0, "total": 0}
