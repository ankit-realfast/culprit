import pytest

from culprit.cli import main
from culprit.store import read_symptom
from records import assistant, human, interrupt, system


def test_triage_prints_and_saves_the_ranked_queue(write_session, tmp_path, capsys):
    write_session([human("clean prompt", "p1", session_id="s1"), assistant(),
                   human("went wrong", "p2", session_id="s1"), assistant(), interrupt("p2")])
    runs = tmp_path / "runs"
    exit_code = main(["triage", "--top", "5"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "s1:p2" in out and "went wrong" in out and "interrupted" in out
    assert "s1:p1" not in out
    assert "2 episodes scanned" in out
    assert (runs / "candidates.json").exists()


def test_confirm_records_a_symptom_for_a_queued_episode(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage"])
    assert main(["confirm", "s1:p2", "stopped-short"]) == 0
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_confirm_before_triage_is_not_ready_with_exit_3(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    assert main(["confirm", "nope:p9", "wasteful"]) == 3
    assert "culprit triage" in capsys.readouterr().err


def test_confirm_refuses_an_unknown_episode_with_exit_1(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage"])
    assert main(["confirm", "s1:p2nope", "wasteful"]) == 1


def test_a_second_triage_keeps_the_queue_and_says_how_many_are_queued(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage"])
    write_session([human("also wrong", "p3", session_id="s2"), interrupt("p3")], name="s2.jsonl")
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    capsys.readouterr()
    main(["triage"])
    out = capsys.readouterr().out
    assert "s2:p3" in out and "s1:p2" not in out
    assert "2 queued" in out
    assert out.lstrip().startswith("#1 ")
    assert "1 more queued, not in this top 1 — see `culprit status`" in out
    assert main(["confirm", "s1:p2", "stopped-short"]) == 1  # its log is gone, so no criterion can be drafted
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_triage_numbers_rows_and_commands_take_the_number(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage"])
    assert "#1 " in capsys.readouterr().out
    assert main(["confirm", "#1", "wasteful"]) == 0
    assert read_symptom(runs, "s1:p2").value == "wasteful"
    assert main(["confirm", "s1", "stopped-short"]) == 0
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_the_store_and_the_logs_come_from_the_environment(write_session, tmp_path, monkeypatch):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    (tmp_path / "projects").rename(tmp_path / "elsewhere")
    monkeypatch.setenv("CULPRIT_RUNS", str(tmp_path / "env-runs"))
    monkeypatch.setenv("CULPRIT_PROJECTS", str(tmp_path / "elsewhere"))
    assert main(["triage"]) == 0
    assert "s1:p2" in (tmp_path / "env-runs" / "candidates.json").read_text()


def test_without_culprit_runs_the_store_is_runs_in_the_current_folder(write_session, tmp_path, monkeypatch):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    monkeypatch.delenv("CULPRIT_RUNS")
    main(["triage"])
    assert (tmp_path / "cwd" / "runs" / "candidates.json").exists()


@pytest.mark.parametrize("argv", [["triage", "--runs", "x"], ["triage", "--projects", "x"],
                                  ["status", "--runs", "x"], ["confirm", "s1:p2", "wasteful", "--runs", "x"],
                                  ["restore", "s1:p2", "--dest", "x"], ["restore", "s1:p2", "--file-history", "x"]])
def test_store_logs_and_file_history_are_not_flags(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2


def test_read_only_commands_refuse_a_missing_store_without_creating_it(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "nowhere"
    monkeypatch.setenv("CULPRIT_RUNS", str(missing))
    for argv in (["review"], ["status"], ["report"], ["confirm", "s1:p1", "wasteful"]):
        assert main(argv) == 3
        err = capsys.readouterr().err
        assert f"no culprit store at {missing.resolve()}" in err and "CULPRIT_RUNS" in err
    assert not missing.exists()


def test_triage_marks_rows_with_api_errors_as_likely_not_reproducible(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), system("api_error"), interrupt("p2")])
    main(["triage"])
    assert "API errors — likely not reproducible" in capsys.readouterr().out


def test_ctrl_c_stops_with_exit_130_and_says_how_to_resume(tmp_path, capsys, monkeypatch):
    import culprit.cli as cli_module

    def interrupted(runs_dir):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "write_report", interrupted)
    (tmp_path / "runs").mkdir()
    assert main(["report"]) == 130
    assert "stopped. finished runs are kept; rerun the same command to resume" in capsys.readouterr().err


def test_triage_rows_are_numbered_contiguously_when_kept_episodes_are_not_shown(write_session, tmp_path, capsys):
    runs = tmp_path / "runs"
    write_session([human("a", "p1", session_id="s1"), interrupt("p1"),
                   human("b", "p2", session_id="s1"), interrupt("p2")])
    main(["triage"])
    write_session([human("c", "p3", session_id="s2"), system("api_error"), interrupt("p3"),
                   human("d", "p4", session_id="s2"), interrupt("p4"), system("compact_boundary")], name="s2.jsonl")
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    capsys.readouterr()
    main(["triage", "--top", "2"])
    rows = [line.split()[0] for line in capsys.readouterr().out.splitlines() if line.lstrip().startswith("#")]
    assert rows == ["#1", "#2"]
    assert main(["confirm", "#1", "wasteful"]) == 0
    assert read_symptom(runs, "s2:p4").value == "wasteful"

