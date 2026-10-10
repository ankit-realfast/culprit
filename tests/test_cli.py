from culprit.cli import main
from culprit.store import read_symptom
from records import assistant, human, interrupt


def test_triage_prints_and_saves_the_ranked_queue(write_session, tmp_path, capsys):
    write_session([human("clean prompt", "p1", session_id="s1"), assistant(),
                   human("went wrong", "p2", session_id="s1"), assistant(), interrupt("p2")])
    runs = tmp_path / "runs"
    exit_code = main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs), "--top", "5"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "s1:p2" in out and "went wrong" in out and "interrupted" in out
    assert "s1:p1" not in out
    assert "2 episodes scanned" in out
    assert (runs / "candidates.json").exists()


def test_confirm_records_a_symptom_for_a_queued_episode(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    assert main(["confirm", "s1:p2", "stopped-short", "--runs", str(runs)]) == 0
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_confirm_before_triage_is_not_ready_with_exit_3(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    assert main(["confirm", "nope:p9", "wasteful", "--runs", str(tmp_path / "runs")]) == 3
    assert "culprit triage" in capsys.readouterr().err


def test_confirm_refuses_an_unknown_episode_with_exit_1(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    assert main(["confirm", "s1:p2nope", "wasteful", "--runs", str(runs)]) == 1


def test_a_second_triage_keeps_the_queue_and_says_how_many_are_queued(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    write_session([human("also wrong", "p3", session_id="s2"), interrupt("p3")], name="s2.jsonl")
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    capsys.readouterr()
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    out = capsys.readouterr().out
    assert "s2:p3" in out and "s1:p2" not in out
    assert "2 queued" in out
    assert main(["confirm", "s1:p2", "stopped-short", "--runs", str(runs)]) == 0


def test_triage_numbers_rows_and_commands_take_the_number(write_session, tmp_path, capsys):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    runs = tmp_path / "runs"
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(runs)])
    assert "#1 " in capsys.readouterr().out
    assert main(["confirm", "#1", "wasteful", "--runs", str(runs)]) == 0
    assert read_symptom(runs, "s1:p2").value == "wasteful"
    assert main(["confirm", "s1", "stopped-short", "--runs", str(runs)]) == 0
    assert read_symptom(runs, "s1:p2").value == "stopped-short"


def test_culprit_runs_sets_the_default_store_and_the_flag_wins(write_session, tmp_path, monkeypatch):
    write_session([human("went wrong", "p2", session_id="s1"), interrupt("p2")])
    monkeypatch.setenv("CULPRIT_RUNS", str(tmp_path / "env-runs"))
    main(["triage", "--projects", str(tmp_path / "projects")])
    assert (tmp_path / "env-runs" / "candidates.json").exists()
    main(["triage", "--projects", str(tmp_path / "projects"), "--runs", str(tmp_path / "flag-runs")])
    assert (tmp_path / "flag-runs" / "candidates.json").exists()


def test_read_only_commands_refuse_a_missing_store_without_creating_it(tmp_path, capsys):
    missing = tmp_path / "nowhere"
    for argv in (["review"], ["calibration"], ["report"], ["confirm", "s1:p1", "wasteful"]):
        assert main([*argv, "--runs", str(missing)]) == 3
        err = capsys.readouterr().err
        assert f"no culprit store at {missing.resolve()}" in err and "CULPRIT_RUNS" in err
    assert not missing.exists()
