import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.store import episode_dir
from culprit.verdict import Verdict
from records import human, snapshot


def ready(write_session, tmp_path, confirm=True, restored=True, repo_exists=True):
    repo = tmp_path / "repo"
    if repo_exists:
        repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo)),
                   human("still wrong", "p2", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    if restored:
        (episode_dir(runs, "s1:p1") / "pristine").mkdir()
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
                       {"baseline": {"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1.0, "void": 0},
                        "harness": {"variant": "harness", "passes": 4, "runs": 5, "cost_usd": 1.0, "void": 0}},
                       {"environment": "no environment fix supplied (culprit env-fix)"})

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    assert cli.main(["diagnose", "s1:p1"]) == 0
    out = capsys.readouterr().out
    assert seen == {"symptom": "wrong-result", "follow_ups": ["still wrong"], "cap": 40.0, "env": None, "n": cli.RUNS_PER_VARIANT}
    assert "cause: harness" in out and "4/5" in out and "environment untested" in out
    assert "harness: baseline 0/5 → 4/5, p = 0.0238" in out


def test_diagnose_before_confirm_names_confirm(write_session, tmp_path, capsys):
    projects, runs = ready(write_session, tmp_path, confirm=False)
    assert cli.main(["diagnose", "s1:p1"]) == 3
    assert "culprit confirm" in capsys.readouterr().err


def test_diagnose_before_confirm_names_triage_when_the_episode_is_not_queued(write_session, tmp_path, capsys):
    projects, runs = ready(write_session, tmp_path, confirm=False)
    cli.main(["diagnose", "s1:p1"])
    assert "culprit triage" in capsys.readouterr().err


def fake_verdict(seen):
    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        seen.append(episode.episode_id)
        return Verdict("s1:p1", "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5", {}, {})
    return fake_diagnose


def test_diagnose_restores_the_starting_files_first_when_they_are_missing(write_session, tmp_path, capsys,
                                                                          monkeypatch):
    projects, runs = ready(write_session, tmp_path, restored=False)
    seen = []
    monkeypatch.setattr(cli, "diagnose", fake_verdict(seen))
    assert cli.main(["diagnose", "s1:p1"]) == 0
    assert (episode_dir(runs, "s1:p1") / "pristine").is_dir()
    assert (episode_dir(runs, "s1:p1") / "restore.json").is_file()
    out = capsys.readouterr().out
    assert "restored from snapshots" in out and "cause: harness" in out
    assert seen == ["s1:p1"]


def test_diagnose_refuses_with_the_reasons_when_the_episode_cannot_be_replayed(write_session, tmp_path, capsys,
                                                                               monkeypatch):
    projects, runs = ready(write_session, tmp_path, restored=False, repo_exists=False)
    seen = []
    monkeypatch.setattr(cli, "diagnose", fake_verdict(seen))
    assert cli.main(["diagnose", "s1:p1"]) == 1
    err = capsys.readouterr().err
    assert "s1:p1 cannot be replayed" in err and "no longer exists" in err
    assert seen == [] and not (episode_dir(runs, "s1:p1") / "pristine").exists()


def test_diagnose_without_an_id_restores_the_ready_episodes_then_diagnoses_the_queue(write_session, tmp_path,
                                                                                     capsys, monkeypatch):
    from culprit.batch import BatchSummary
    projects, runs = ready(write_session, tmp_path, restored=False)
    (runs / "candidates.json").write_text('[{"score": 1, "summary": {"episode_id": "s1:p1"}}]')
    seen = []

    def fake_all(runs_dir, projects_dir, **kwargs):
        seen.append((episode_dir(runs_dir, "s1:p1") / "pristine").is_dir())
        return BatchSummary(diagnosed=["s1:p1"])

    monkeypatch.setattr(cli, "diagnose_all", fake_all)
    assert cli.main(["diagnose"]) == 0
    assert seen == [True]
    out = capsys.readouterr().out
    assert "restored s1:p1" in out and "diagnosed 1" in out


def test_diagnose_all_is_gone(capsys):
    import pytest
    with pytest.raises(SystemExit) as stopped:
        cli.main(["diagnose-all"])
    assert stopped.value.code == 2
