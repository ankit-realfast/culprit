"""The What happened recap: what the person asked, what Claude did, how the episode ended and what the person said
next, built from the episode's own records with no model call. Synthetic records only."""

import json

import culprit.cli as cli
from culprit.logs import episodes_from_session, what_happened
from culprit.report import what_happened_block
from records import assistant, human, interrupt, snapshot, system, tool_use

ASK = "when any filter is active on the people dashboard, the list gets hidden; fix it"


def error_result(text):
    return {"type": "user", "promptId": "p1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": [{"type": "text", "text": text}]}]}}


def ok_result():
    return {"type": "user", "promptId": "p1",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def at(record, time):
    return {**record, "timestamp": f"2026-10-01T10:{time}Z"}


def busy_episode(repo):
    """Read, edit two files, run tests that fail twice, explain, then the person interrupts after 8 minutes."""
    return [
        human(ASK, "p1", cwd=str(repo), timestamp="2026-10-01T10:00:00Z"),
        tool_use("Read", file_path=f"{repo}/dashboard/people.js"), ok_result(),
        tool_use("Edit", file_path=f"{repo}/dashboard/people.js"), ok_result(),
        tool_use("Edit", file_path=f"{repo}/api.js"), ok_result(),
        tool_use("Edit", file_path=f"{repo}/api.js"), ok_result(),
        tool_use("Bash", command="npm test"),
        error_result("<tool_use_error>Error: 3 tests failed in people.test.js\n  at line 4</tool_use_error>"),
        tool_use("Bash", command="npm test"), error_result("Error: 2 tests failed"),
        assistant(text="I kept the list visible while filtering; the loading test still fails."),
        at(interrupt("p1"), "08:00"),
    ]


def recap_of(records, write_session, next_prompt=None):
    path = write_session(records)
    return what_happened(next(episodes_from_session(path)), next_prompt)


def test_the_recap_counts_tool_calls_names_edited_files_and_quotes_the_first_error(tmp_path, write_session):
    recap = recap_of(busy_episode(tmp_path / "repo"), write_session, "still hidden")
    assert recap.asked == ASK
    assert recap.did == "6 tool calls · edited dashboard/people.js, api.js · 2 tool errors"
    assert recap.first_error == "Error: 3 tests failed in people.test.js"
    assert recap.last_text == "I kept the list visible while filtering; the loading test still fails."
    assert recap.ended == "you interrupted it after 8 min"
    assert recap.said_next == "still hidden"


def test_more_than_five_edited_files_are_counted_past_the_fifth(tmp_path, write_session):
    edits = [tool_use("Write", file_path=f"{tmp_path}/f{i}.py") for i in range(7)]
    recap = recap_of([human("make files", "p1", cwd=str(tmp_path)), *edits], write_session)
    assert recap.did == "7 tool calls · edited f0.py, f1.py, f2.py, f3.py, f4.py +2 more"
    assert recap.first_error is None and recap.said_next is None


def test_a_turn_that_finished_and_one_that_api_errors_ended_say_so(tmp_path, write_session):
    finished = recap_of([human("hi", "p1"), assistant(text="done"), system("turn_duration", durationMs=180_000)],
                        write_session)
    assert finished.ended == "it finished after 3 min" and finished.did == "no tool calls"
    errored = recap_of([human("hi", "p1"), assistant(text="working"), system("api_error")], write_session,)
    assert errored.ended == "API errors ended it"


def test_the_block_aligns_its_labels_and_wraps_under_the_values(tmp_path, write_session, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    recap = recap_of(busy_episode(tmp_path / "repo"), write_session, "still hidden")
    assert what_happened_block(recap) == [
        "What happened",
        f'  You asked      "{ASK}"',
        "  Claude did     6 tool calls · edited dashboard/people.js, api.js · 2 tool errors",
        '                 (first: "Error: 3 tests failed in people.test.js")',
        '  Claude\'s last  "I kept the list visible while filtering; the loading test still fails."',
        "  It ended       you interrupted it after 8 min",
        '  You said next  "still hidden"',
    ]


def test_long_text_is_cut_and_wrapped(tmp_path, write_session, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    recap = recap_of([human("word " * 100, "p1")], write_session)
    lines = what_happened_block(recap)
    asked = [line for line in lines[1:] if not line.startswith("  Claude") and not line.startswith("  It")]
    assert all(len(line) <= 60 for line in asked) and all(line.startswith(" " * 17) for line in asked[1:])
    assert len(recap.asked) <= 300 and recap.asked.endswith("…")


def queued_busy_episode(write_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), *busy_episode(repo), human("still hidden", "p2", cwd=str(repo))])
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "candidates.json").write_text(json.dumps([{"score": 9, "summary": {
        "episode_id": "s1:p1", "prompt_excerpt": "fix it",
        "signals": {"interrupted": True, "tool_errors": 2, "api_errors": 0, "compactions": 0,
                    "duration_s": 480.0}}}]))
    return runs


def test_confirm_with_a_symptom_shows_what_happened_before_recording_it(write_session, tmp_path, capsys):
    queued_busy_episode(write_session, tmp_path)
    assert cli.main(["confirm", "1", "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert out.index("Episode 1 · s1:p1") < out.index("What happened\n") < out.index("Recorded what went wrong")
    assert "  It ended       you interrupted it after 8 min\n" in out


def test_status_of_one_episode_shows_what_happened_above_its_progress(write_session, tmp_path, capsys):
    queued_busy_episode(write_session, tmp_path)
    assert cli.main(["status", "1"]) == 0
    out = capsys.readouterr().out
    assert out.index("What happened\n") < out.index("Progress\n")
    assert '  You said next  "still hidden"\n' in out


def test_status_without_the_session_log_leaves_what_happened_out(write_session, tmp_path, capsys):
    queued_busy_episode(write_session, tmp_path)
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    assert cli.main(["status", "1"]) == 0
    assert "What happened" not in capsys.readouterr().out


def test_the_walk_shows_what_happened_for_each_episode(write_session, tmp_path, capsys, monkeypatch):
    queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "quit")
    assert cli.main(["confirm"]) == 0
    out = capsys.readouterr().out
    assert out.index("Episode 1 · s1:p1") < out.index("What happened\n")
    assert "  Claude did     6 tool calls · edited dashboard/people.js, api.js · 2 tool errors\n" in out
