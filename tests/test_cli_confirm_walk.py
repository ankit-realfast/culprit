"""`culprit confirm` with no episode id: walk the queue, asking the symptom, approving the criterion, restoring."""

import json

import culprit.cli as cli
from culprit.model import Criterion, load_criterion, save_criterion
from culprit.store import Symptom, episode_dir, read_symptom, record_symptom
from fakes import FakeAsk
from records import edit_result, human, snapshot


def answering(monkeypatch, *answers):
    """A terminal on stdin that types these answers, one per question; asking past them fails the test."""
    queue = list(answers)
    asked = []

    def typed(prompt=""):
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"asked one question too many: {prompt}")
        answer = queue.pop(0)
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", typed)
    return queue, asked


def two_queued(write_session, tmp_path):
    """s1:p1 can be replayed (its repo exists, its file history is there); s2:p1's working directory is gone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("new\n")
    history = tmp_path / "file-history" / "s1"
    history.mkdir(parents=True)
    (history / "aaa@v1").write_text("old\n")
    write_session([snapshot("u-p1", {"a.py": "aaa@v1"}), human("fix a", "p1", session_id="s1", cwd=str(repo)),
                   edit_result("p1", str(repo / "a.py"), "old\n"),
                   human("no, still broken", "p2", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    write_session([human("tidy b", "p1", session_id="s2", cwd=str(tmp_path / "gone")),
                   human("that's wrong", "p2", session_id="s2", cwd=str(tmp_path / "gone"))], name="s2.jsonl")
    runs = tmp_path / "runs"
    runs.mkdir()
    signals = {"interrupted": False, "tool_errors": 2, "api_errors": 0, "compactions": 0, "duration_s": 30.0}
    (runs / "candidates.json").write_text(json.dumps([
        {"score": 9, "summary": {"episode_id": "s1:p1", "prompt_excerpt": "fix a", "signals": signals}},
        {"score": 5, "summary": {"episode_id": "s2:p1", "prompt_excerpt": "tidy b", "signals": signals}}]))
    return runs


class Drafts(FakeAsk):
    """Answers criterion drafts in order (counted in calls) and every symptom suggestion with wrong-result."""

    def __init__(self, *responses):
        super().__init__(*responses)
        self.suggested = []

    def __call__(self, prompt, schema):
        if "symptom" in schema["properties"]:
            self.suggested.append(prompt)
            return {"symptom": "wrong-result", "reason": "you said next that it was still broken"}
        return super().__call__(prompt, schema)


def drafts(monkeypatch, n=2):
    fake = Drafts(*[{"success_means": f"works {i}", "checks": [], "must_not": []} for i in range(n)])
    monkeypatch.setattr(cli, "ask_claude", fake)
    return fake


def test_without_a_terminal_confirm_needs_an_id_and_a_symptom(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(AssertionError("asked")))
    assert cli.main(["confirm"]) == 3
    assert "culprit confirm <episode-id> <symptom>" in capsys.readouterr().err
    assert read_symptom(runs, "s1:p1") is None


def test_the_walk_confirms_approves_and_restores_each_episode_in_queue_order(write_session, tmp_path, capsys,
                                                                             monkeypatch):
    runs = two_queued(write_session, tmp_path)
    fake = drafts(monkeypatch)
    left, asked = answering(monkeypatch, "wrong-result", "a", "2", "a")
    assert cli.main(["confirm"]) == 0
    captured = capsys.readouterr()
    assert left == [] and len(fake.calls) == 2
    assert read_symptom(runs, "s1:p1") is Symptom.WRONG_RESULT and read_symptom(runs, "s2:p1") is Symptom.WRONG_RESULT
    assert load_criterion(runs, "s1:p1").approved and load_criterion(runs, "s2:p1").approved
    assert (episode_dir(runs, "s1:p1") / "pristine" / "a.py").read_text() == "old\n"
    assert not (episode_dir(runs, "s2:p1") / "pristine").exists()
    out = captured.out
    assert out.index("Episode 1 · s1:p1\n") < out.index("fix a") < out.index("Episode 2 · s2:p1\n") < out.index("tidy b")
    assert "What happened\n  You asked      \"fix a\"\n  Claude did     no tool calls\n" in out
    assert '  You said next  "no, still broken"' in out and '  You said next  "that\'s wrong"' in out
    assert "Draft success criterion — not approved yet\n  Success means\n    works 0" in out
    assert "✓ Approved the success criterion" in out and "1 restored from snapshots" in out
    assert "Not replayable: working directory" in out
    assert out.endswith("Next\n  1. Diagnose every confirmed episode:  culprit diagnose\n")
    assert "next:" not in captured.err


def test_episodes_already_past_every_step_are_not_shown(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    record_symptom(runs, "s1:p1", Symptom.WRONG_RESULT)
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir()
    record_symptom(runs, "s2:p1", Symptom.STOPPED_SHORT)
    save_criterion(runs, "s2:p1", Criterion("draft", (), (), approved=False))
    drafts(monkeypatch, 0)
    left, asked = answering(monkeypatch, "a")  # only s2:p1's draft is waiting, and its symptom is known
    assert cli.main(["confirm"]) == 0
    out = capsys.readouterr().out
    assert "fix a" not in out and "✓ What went wrong: stopped-short (recorded earlier)" in out
    assert left == [] and len(asked) == 1 and load_criterion(runs, "s2:p1").approved


def test_an_unreplayable_episode_that_is_confirmed_and_approved_is_done(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    for episode_id in ("s1:p1", "s2:p1"):
        record_symptom(runs, episode_id, Symptom.WRONG_RESULT)
        save_criterion(runs, episode_id, Criterion("works", (), (), approved=True))
    (episode_dir(runs, "s1:p1") / "pristine").mkdir()
    answering(monkeypatch)
    assert cli.main(["confirm"]) == 0
    assert "Nothing left to confirm" in capsys.readouterr().out


def test_skip_moves_on_and_quit_stops(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    fake = drafts(monkeypatch)
    left, asked = answering(monkeypatch, "skip", "q")  # "s" alone would be ambiguous with stopped-short
    assert cli.main(["confirm"]) == 0
    assert left == [] and fake.calls == [] and len(asked) == 2 and "6 skip · 7 quit" in asked[0]
    assert read_symptom(runs, "s1:p1") is None and read_symptom(runs, "s2:p1") is None


def test_edit_later_leaves_the_draft_and_does_not_restore(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    drafts(monkeypatch)
    answering(monkeypatch, "wasteful", "e", "q")
    assert cli.main(["confirm"]) == 0
    out = capsys.readouterr().out
    assert load_criterion(runs, "s1:p1").approved is False
    assert not (episode_dir(runs, "s1:p1") / "pristine").exists()
    assert "Edit" in out and "/s1__p1/criterion.json" in out


def test_an_unknown_answer_is_asked_again_and_end_of_input_quits(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    drafts(monkeypatch)
    left, asked = answering(monkeypatch, "broken", "1", "maybe", EOFError)
    assert cli.main(["confirm"]) == 0
    assert left == [] and len(asked) == 4
    assert read_symptom(runs, "s1:p1") is Symptom.STOPPED_SHORT and not load_criterion(runs, "s1:p1").approved
    assert read_symptom(runs, "s2:p1") is None


def test_confirm_with_an_id_but_no_symptom_names_the_symptoms(write_session, tmp_path, capsys):
    runs = two_queued(write_session, tmp_path)
    assert cli.main(["confirm", "s1:p1"]) == 0  # the default fake answers no suggestion
    out = capsys.readouterr().out
    assert "Suggested" not in out and "  no-failure            nothing went wrong" in out
    assert out.endswith("Next\n  1. Say what went wrong:  culprit confirm s1:p1 <symptom>\n")
    assert read_symptom(runs, "s1:p1") is None


def test_an_episode_whose_log_is_gone_keeps_its_symptom_and_the_walk_goes_on(write_session, tmp_path, capsys,
                                                                             monkeypatch):
    runs = two_queued(write_session, tmp_path)
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()
    drafts(monkeypatch, 1)
    left, _ = answering(monkeypatch, "wrong-result", "q")
    assert cli.main(["confirm"]) == 0
    out = capsys.readouterr().out
    assert read_symptom(runs, "s1:p1") is Symptom.WRONG_RESULT and load_criterion(runs, "s1:p1") is None
    assert "Criterion not drafted" in out and "tidy b" in out and left == []


def test_a_row_stored_with_the_retired_corrected_signal_still_shows(write_session, tmp_path, capsys, monkeypatch):
    runs = two_queued(write_session, tmp_path)
    (tmp_path / "projects" / "-repo" / "s1.jsonl").unlink()  # without its log, the walk shows what triage saw
    rows = json.loads((runs / "candidates.json").read_text())
    rows[0]["summary"]["signals"]["corrected"] = "no, still broken"
    (runs / "candidates.json").write_text(json.dumps(rows))
    drafts(monkeypatch)
    answering(monkeypatch, "q")
    assert cli.main(["confirm"]) == 0
    assert "2 tool errors" in capsys.readouterr().out
