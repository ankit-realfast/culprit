import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.store import episode_dir
from culprit.verdict import Verdict
from records import human, snapshot


def ready(write_session, tmp_path, confirm=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo)),
                   human("still wrong", "p2", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    if confirm:
        (episode_dir(runs, "s1:p1") / "symptom.json").write_text('{"episode_id": "s1:p1", "symptom": "wrong-result"}')
    return tmp_path / "projects", runs


def test_env_fix_stores_the_script_for_the_environment_oracle(write_session, tmp_path):
    projects, runs = ready(write_session, tmp_path)
    script = tmp_path / "fix.sh"
    script.write_text("export FLAKY=0\n")
    assert cli.main(["env-fix", "s1:p1", str(script)]) == 0
    assert (episode_dir(runs, "s1:p1") / "env-fix.sh").read_text() == "export FLAKY=0\n"


def test_diagnose_passes_everything_through_and_prints_the_verdict(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = ready(write_session, tmp_path)
    seen = {}

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        seen.update(symptom=symptom.value, follow_ups=kwargs["follow_ups"], cap=kwargs["budget"].cap_usd,
                    env=kwargs["env_script"], n=kwargs["n"])
        return Verdict("s1:p1", "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5",
                       {}, {"environment": "no environment fix supplied (culprit env-fix)"})

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    assert cli.main(["diagnose", "s1:p1"]) == 0
    out = capsys.readouterr().out
    assert seen == {"symptom": "wrong-result", "follow_ups": ["still wrong"], "cap": 40.0, "env": None, "n": 5}
    assert "cause: harness" in out and "4/5" in out and "environment untested" in out


def test_diagnose_before_confirm_names_confirm(write_session, tmp_path, capsys):
    projects, runs = ready(write_session, tmp_path, confirm=False)
    assert cli.main(["diagnose", "s1:p1"]) == 3
    assert "culprit confirm" in capsys.readouterr().err


def test_diagnose_before_confirm_names_triage_when_the_episode_is_not_queued(write_session, tmp_path, capsys):
    projects, runs = ready(write_session, tmp_path, confirm=False)
    cli.main(["diagnose", "s1:p1"])
    assert "culprit triage" in capsys.readouterr().err
