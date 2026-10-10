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


def test_confirm_refuses_an_unknown_episode_with_exit_1(tmp_path, capsys):
    assert main(["confirm", "nope:p9", "wasteful", "--runs", str(tmp_path / "runs")]) == 1
    assert "culprit triage" in capsys.readouterr().err


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
