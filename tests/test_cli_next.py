import json

import culprit.cli as cli
from culprit.model import Criterion, save_criterion
from culprit.diagnose import VariantOutcome, Verdict
from culprit.store import episode_dir
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


def test_confirm_names_the_approval_next_then_diagnose(write_session, tmp_path, capsys):
    _, runs = queued(write_session, tmp_path)
    assert cli.main(["confirm", "s1:p1", "wrong-result"]) == 0
    assert last_err_line(capsys) == ("next: review it with `culprit status s1:p1`, then rerun "
                                     "`culprit confirm s1:p1 wrong-result` to approve it")
    assert cli.main(["confirm", "s1:p1", "wrong-result"]) == 0
    assert last_err_line(capsys) == "next: run `culprit diagnose s1:p1`"


def test_a_symptom_without_a_criterion_names_confirm_to_draft_it(write_session, tmp_path, capsys):
    from culprit.status import next_step
    _, runs = queued(write_session, tmp_path)
    (episode_dir(runs, "s1:p1")).mkdir()
    (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wasteful"}')
    assert next_step(runs, "s1:p1") == "run `culprit confirm s1:p1 wasteful` to draft what success means"


def test_diagnose_and_env_fix_name_their_next_step(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = queued(write_session, tmp_path)
    cli.main(["confirm", "s1:p1", "wrong-result"])
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir()
    script = tmp_path / "fix.sh"
    script.write_text("true\n")
    capsys.readouterr()
    assert cli.main(["env-fix", "s1:p1", str(script)]) == 0
    assert last_err_line(capsys) == "next: run `culprit diagnose s1:p1`"

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        verdict = Verdict("s1:p1", "wrong-result", "model", "model passed 4/5", {}, {})
        from culprit.diagnose import save_verdict
        save_verdict(episode_dir(runs, "s1:p1"), verdict)
        return verdict

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    assert cli.main(["diagnose", "s1:p1"]) == 0
    assert last_err_line(capsys) == "next: run `culprit report`"
