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
    assert cli.main(["criterion", "draft", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 0
    assert "still broken" in fake.calls[0][0]
    assert cli.main(["criterion", "show", "s1:p1", "--runs", str(runs)]) == 0
    assert "a.py works" in capsys.readouterr().out
    assert cli.main(["criterion", "approve", "s1:p1", "--runs", str(runs)]) == 0
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
    exit_code = cli.main(["restore", "s1:p1", "--projects", str(projects), "--runs", str(runs),
                          "--file-history", str(tmp_path / "file-history")])
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
    assert cli.main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(tmp_path / "runs")]) == 0
    assert "not replayable: working directory" in capsys.readouterr().out


def test_restore_refuses_an_unreplayable_episode(write_session, tmp_path, capsys):
    projects = projects_with_one_episode(write_session, tmp_path, tmp_path / "gone")
    exit_code = cli.main(["restore", "s1:p1", "--projects", str(projects), "--runs", str(tmp_path / "runs")])
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
    cli.main(["restore", "s1:p1", "--projects", str(projects), "--runs", str(runs),
              "--file-history", str(tmp_path / "file-history")])
    stored = json.loads((runs / "s1__p1" / "restore.json").read_text())
    assert stored == {"git_commit": None, "set_from_git": 0, "removed_by_git": 0, "snapshot_agreed": 1, "checked": 1, "restored": 1, "unknown": 0, "missing_backups": 0,
                      "outside_cwd": 0, "set_from_episode": 1, "removed": 0}


def test_criterion_approve_or_show_before_draft_is_not_ready(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert cli.main(["criterion", "approve", "s1:p1", "--runs", str(runs)]) == 3
    assert cli.main(["criterion", "show", "s1:p1", "--runs", str(runs)]) == 3
    assert "culprit criterion draft" in capsys.readouterr().err
