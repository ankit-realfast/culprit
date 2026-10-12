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
    assert "Scanned 2 episodes" in out
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
    assert "the top 1 is row 1 of the 2 queued; 1 more is queued but not shown — see culprit status" in out
    assert out.splitlines()[1].split()[0] == "1"
    assert main(["confirm", "s1:p2", "stopped-short"]) == 1  # its log is gone, so no criterion can be drafted
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_triage_numbers_rows_and_commands_take_the_number(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage"])
    assert capsys.readouterr().out.splitlines()[1].split()[0] == "1"
    assert main(["confirm", "1", "wasteful"]) == 0
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
    rows = [line.split()[0] for line in capsys.readouterr().out.splitlines() if len(line.split()) > 2 and line.split()[0].isdigit() and ":" in line.split()[2]]
    assert rows == ["1", "2"]
    assert main(["confirm", "1", "wasteful"]) == 0
    assert read_symptom(runs, "s2:p4").value == "wasteful"



def test_triage_warns_once_about_models_of_a_family_culprit_does_not_know(write_session, tmp_path, capsys):
    write_session([human("a", "p1", session_id="s1"), assistant(model="claude-mythos-1"), interrupt("p1"),
                   human("b", "p2", session_id="s1"), assistant(model="claude-mythos-1"),
                   human("c", "p3", session_id="s1"), assistant(model="claude-opus-4-8")])
    main(["triage"])
    err = capsys.readouterr().err
    assert err.count("warning:") == 1
    assert ("your sessions used claude-mythos-1; if it is stronger than fable, set "
            "CULPRIT_ORACLE_MODEL=claude-mythos-1") in err


def test_the_triage_warning_names_what_the_oracle_resolved_to_and_every_unknown_id(write_session, tmp_path, capsys):
    import json
    import time
    write_session([human("a", "p1", session_id="s1"), assistant(model="claude-mythos-1"), interrupt("p1"),
                   human("b", "p2", session_id="s1"), assistant(model="claude-zeta-2")])
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "models.json").write_text(json.dumps({"fable": {"model": "claude-fable-5-1",
                                                                         "at": time.time()}}))
    main(["triage"])
    assert ("your sessions used claude-mythos-1, claude-zeta-2; if one is stronger than claude-fable-5-1, set "
            "CULPRIT_ORACLE_MODEL=<its id>") in capsys.readouterr().err


def test_triage_says_nothing_about_known_families_or_the_model_already_chosen(write_session, monkeypatch, capsys):
    write_session([human("a", "p1", session_id="s1"), assistant(model="claude-mythos-1"), interrupt("p1"),
                   human("b", "p2", session_id="s1"), assistant(model="claude-sonnet-4-6"),
                   human("c", "p3", session_id="s1"), assistant(model="<synthetic>")])
    monkeypatch.setenv("CULPRIT_ORACLE_MODEL", "claude-mythos-1")
    main(["triage"])
    assert "warning" not in capsys.readouterr().err
