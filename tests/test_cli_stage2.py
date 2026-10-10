import json

import pytest

import culprit.cli as cli
from culprit.criterion import load_criterion
from culprit.store import read_symptom
from fakes import FakeAsk
from records import edit_result, human, interrupt, snapshot


def projects_with_one_episode(write_session, tmp_path, repo):
    write_session([snapshot("u-p1", {"a.py": "aaa@v1"}), human("fix a", "p1", cwd=str(repo)),
                   edit_result("p1", str(repo / "a.py"), "old\n"), human("no, still broken", "p2", cwd=str(repo))],
                  name="s1.jsonl")
    return tmp_path / "projects"


def queued_episode(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "candidates.json").write_text(json.dumps([{"score": 5, "summary": {"episode_id": "s1:p1"}}]))
    return repo, runs


def test_confirm_drafts_the_criterion_shows_it_and_a_second_confirm_approves_it(write_session, tmp_path, capsys,
                                                                               monkeypatch):
    _, runs = queued_episode(write_session, tmp_path)
    fake = FakeAsk({"success_means": "a.py works", "checks": ["pytest -q"], "must_not": ["skip tests"]})
    monkeypatch.setattr(cli, "ask_claude", fake)
    assert cli.main(["confirm", "s1:p1", "wrong-result"]) == 0
    assert "still broken" in fake.calls[0][0]
    out = capsys.readouterr().out
    assert str(runs / "s1__p1" / "criterion.json") in out
    assert "criterion [draft, not approved]: a.py works" in out
    assert "check: pytest -q" in out and "must not: skip tests" in out
    assert "approve: rerun `culprit confirm s1:p1 wrong-result`" in out
    assert load_criterion(runs, "s1:p1").approved is False
    assert cli.main(["confirm", "s1:p1", "wrong-result"]) == 0
    assert len(fake.calls) == 1  # never redrafted
    assert "approved the criterion" in capsys.readouterr().out
    assert load_criterion(runs, "s1:p1").approved is True


def test_confirm_with_a_different_symptom_corrects_it_without_approving(write_session, tmp_path, capsys):
    _, runs = queued_episode(write_session, tmp_path)
    cli.main(["confirm", "s1:p1", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["confirm", "s1:p1", "stopped-short"]) == 0
    assert read_symptom(runs, "s1:p1").value == "stopped-short"
    assert load_criterion(runs, "s1:p1").approved is False
    assert "approve: rerun `culprit confirm s1:p1 stopped-short`" in capsys.readouterr().out


def test_confirm_never_overwrites_a_criterion(write_session, tmp_path, capsys, monkeypatch):
    _, runs = queued_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk({"success_means": "first", "checks": [], "must_not": []}))
    cli.main(["confirm", "s1:p1", "wrong-result"])
    cli.main(["confirm", "s1:p1", "wrong-result"])
    second = FakeAsk({"success_means": "second", "checks": [], "must_not": []})
    monkeypatch.setattr(cli, "ask_claude", second)
    assert cli.main(["confirm", "s1:p1", "stopped-short"]) == 0
    assert second.calls == [] and load_criterion(runs, "s1:p1").success_means == "first"
    assert load_criterion(runs, "s1:p1").approved is True


def test_confirm_records_the_symptom_even_when_the_log_is_gone(write_session, tmp_path, capsys):
    _, runs = queued_episode(write_session, tmp_path)
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    assert cli.main(["confirm", "s1:p1", "wrong-result"]) == 1
    assert read_symptom(runs, "s1:p1").value == "wrong-result" and load_criterion(runs, "s1:p1") is None


def test_restore_builds_the_pristine_copy_and_reports_the_check(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    out = "\n".join(cli.restore_episode(runs, projects, "s1:p1"))
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


def test_restore_refuses_an_unreplayable_episode(write_session, tmp_path):
    projects = projects_with_one_episode(write_session, tmp_path, tmp_path / "gone")
    with pytest.raises(cli.NotReplayable, match="no longer exists"):
        cli.restore_episode(tmp_path / "runs", projects, "s1:p1")
    assert not (tmp_path / "runs" / "s1__p1" / "pristine").exists()


def test_restore_stores_its_trust_figures_for_the_report(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    projects = projects_with_one_episode(write_session, tmp_path, repo)
    runs = tmp_path / "runs"
    cli.restore_episode(runs, projects, "s1:p1")
    stored = json.loads((runs / "s1__p1" / "restore.json").read_text())
    assert stored == {"git_commit": None, "set_from_git": 0, "removed_by_git": 0, "snapshot_agreed": 1, "checked": 1,
                      "restored": 1, "unknown": 0, "missing_backups": 0, "outside_cwd": 0, "set_from_episode": 1,
                      "removed": 0}


@pytest.mark.parametrize("argv", [["baseline", "s1:p1"], ["restore", "s1:p1"], ["criterion", "draft", "s1:p1"],
                                  ["criterion", "approve", "s1:p1"], ["criterion", "show", "s1:p1"]])
def test_baseline_restore_and_criterion_are_not_commands(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2


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
    rows = [line for line in lines if len(line.split()) > 2 and line.split()[0].isdigit() and ":" in line.split()[2]]
    assert "s1:p1" in rows[0] and rows[0].split()[0] == "1"
    assert "s9:p9" in rows[1] and rows[1].split()[0] == "2"
    assert "not replayable: working directory" in lines[lines.index(rows[1]) + 1]
    assert cli.main(["confirm", "1", "wasteful"]) == 0
    assert read_symptom(runs, "s1:p1").value == "wasteful"
