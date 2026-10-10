import culprit.cli as cli
from culprit.criterion import load_criterion
from fakes import FakeAsk
from records import edit_result, human, interrupt, snapshot


def projects_with_one_episode(write_session, tmp_path, repo):
    write_session([snapshot("u-p1", {"a.py": "aaa@v1"}), human("fix a", "p1", cwd=str(repo)),
                   edit_result("p1", str(repo / "a.py"), "old\n"), human("no, still broken", "p2", cwd=str(repo))],
                  name="s1.jsonl")
    return tmp_path / "projects"


def test_criterion_draft_show_approve(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    fake = FakeAsk({"success_means": "a.py works", "checks": [], "must_not": []})
    monkeypatch.setattr(cli, "ask_claude", fake)
    assert cli.main(["criterion", "draft", "s1:p1"]) == 0
    assert "still broken" in fake.calls[0][0]
    assert cli.main(["criterion", "show", "s1:p1"]) == 0
    assert "a.py works" in capsys.readouterr().out
    assert cli.main(["criterion", "approve", "s1:p1"]) == 0
    assert load_criterion(runs, "s1:p1").approved is True


def test_restore_builds_the_pristine_copy_and_reports_the_check(write_session, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    exit_code = cli.main(["restore", "s1:p1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert (runs / "s1__p1" / "pristine" / "a.py").read_text() == "old\n"
    assert "1 restored from snapshots" in out
    assert "snapshot agrees with the episode's own record for 1 of 1 files" in out
    assert "1 file(s) set from the episode's own record" in out
    assert "no project hooks" in out


def test_triage_says_why_a_candidate_cannot_be_replayed(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p1", session_id="s1", cwd=str(tmp_path / "gone")), interrupt("p1")],
                  name="s1.jsonl")
    assert cli.main(["triage"]) == 0
    assert "not replayable: working directory" in capsys.readouterr().out


def test_restore_refuses_an_unreplayable_episode(write_session, tmp_path, capsys):
    projects = projects_with_one_episode(write_session, tmp_path, tmp_path / "gone")
    exit_code = cli.main(["restore", "s1:p1"])
    assert exit_code == 1
    assert "no longer exists" in capsys.readouterr().err


def test_restore_stores_its_trust_figures_for_the_report(write_session, tmp_path, capsys):
    import json
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    cli.main(["restore", "s1:p1"])
    stored = json.loads((runs / "s1__p1" / "restore.json").read_text())
    assert stored == {"git_commit": None, "set_from_git": 0, "removed_by_git": 0, "snapshot_agreed": 1, "checked": 1, "restored": 1, "unknown": 0, "missing_backups": 0,
                      "outside_cwd": 0, "set_from_episode": 1, "removed": 0}


def test_criterion_approve_or_show_before_draft_is_not_ready(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert cli.main(["criterion", "approve", "s1:p1"]) == 3
    assert cli.main(["criterion", "show", "s1:p1"]) == 3
    assert "culprit criterion draft" in capsys.readouterr().err


def test_criterion_draft_refuses_to_overwrite_unless_forced(write_session, tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    monkeypatch.setattr(cli, "ask_claude", FakeAsk({"success_means": "first", "checks": [], "must_not": []}))
    cli.main(["criterion", "draft", "s1:p1"])
    cli.main(["criterion", "approve", "s1:p1"])
    capsys.readouterr()
    second = FakeAsk({"success_means": "second", "checks": [], "must_not": []})
    monkeypatch.setattr(cli, "ask_claude", second)
    assert cli.main(["criterion", "draft", "s1:p1"]) == 1
    err = capsys.readouterr().err
    assert "culprit criterion show s1:p1" in err and "--force" in err
    assert second.calls == [] and load_criterion(runs, "s1:p1").success_means == "first"
    assert cli.main(["criterion", "draft", "s1:p1", "--force"]) == 0
    assert load_criterion(runs, "s1:p1") .success_means == "second"
    assert load_criterion(runs, "s1:p1").approved is False


def test_triage_lists_unreplayable_candidates_after_every_replayable_one(write_session, tmp_path, capsys):
    from records import system
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([human("cannot replay", "p9", session_id="s9", cwd=str(tmp_path / "gone")), interrupt("p9"),
                   system("compact_boundary"), system("compact_boundary")], name="s9.jsonl")
    write_session([snapshot("u-p1", {"a.py": "aaa@v1"}), human("can replay", "p1", cwd=str(repo)), interrupt("p1")],
                  name="s1.jsonl")
    runs = tmp_path / "runs"
    assert cli.main(["triage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = [line for line in lines if line.lstrip().startswith("#")]
    assert "s1:p1" in rows[0] and rows[0].lstrip().startswith("#1 ")
    assert "s9:p9" in rows[1] and rows[1].lstrip().startswith("#2 ")
    assert "not replayable: working directory" in lines[lines.index(rows[1]) + 1]
    assert cli.main(["confirm", "#1", "wasteful"]) == 0
    from culprit.store import read_symptom
    assert read_symptom(runs, "s1:p1").value == "wasteful"
