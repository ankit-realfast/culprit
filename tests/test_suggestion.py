"""The suggested symptom: one model call shown only the What happened facts, cached in the episode's folder,
offered as the default the person accepts with Enter and never recorded without them. Synthetic records only."""

import culprit.cli as cli
from culprit.diagnose import stored_spend
from culprit.logs import episodes_from_session, what_happened
from culprit.model import (CALL_CAP_USD, ClaudeCallFailed, SUGGESTION_SCHEMA, load_suggestion, suggest_symptom,
                           suggestion_prompt)
from culprit.store import SYMPTOM_MEANINGS, Symptom, episode_dir, read_symptom, record_symptom
from fakes import FakeAsk
from records import human
from test_what_happened import queued_busy_episode

REASON = 'it edited api.js but the tests still failed ("Error: 3 tests failed in people.test.js")'
SUGGEST = {"symptom": "wrong-result", "reason": REASON}
DRAFT = {"success_means": "works", "checks": [], "must_not": []}


def terminal(monkeypatch, *answers):
    left, asked = list(answers), []

    def typed(prompt=""):
        asked.append(prompt)
        return left.pop(0)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", typed)
    return left, asked


def test_every_symptom_has_a_one_line_meaning():
    assert list(SYMPTOM_MEANINGS) == list(Symptom)
    assert SYMPTOM_MEANINGS[Symptom.STOPPED_SHORT] == "quit before finishing"


def test_the_prompt_shows_only_the_facts_and_the_five_symptoms(tmp_path, write_session):
    path = write_session([human("fix the filter", "p1")])
    facts = what_happened(next(episodes_from_session(path)), "still hidden").as_text()
    prompt = suggestion_prompt(facts)
    assert 'You asked: "fix the filter"' in prompt and 'You said next: "still hidden"' in prompt
    assert all(f"{symptom.value}: {meaning}" in prompt for symptom, meaning in SYMPTOM_MEANINGS.items())
    assert "criterion" not in prompt.lower()
    assert SUGGESTION_SCHEMA["properties"]["symptom"]["enum"] == [s.value for s in Symptom]


def test_a_suggestion_needs_a_known_symptom_and_a_reason():
    assert suggest_symptom("facts", FakeAsk(SUGGEST)).symptom is Symptom.WRONG_RESULT
    for bad in ({"symptom": "broken", "reason": "x"}, {"symptom": "wasteful", "reason": " "}, DRAFT):
        try:
            suggest_symptom("facts", FakeAsk(bad))
        except ClaudeCallFailed:
            continue
        raise AssertionError(f"accepted {bad}")


def test_without_a_terminal_confirm_shows_the_suggestion_and_records_nothing(write_session, tmp_path, capsys,
                                                                              monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")  # the reason wraps, under the symptom
    runs = queued_busy_episode(write_session, tmp_path)
    fake = FakeAsk(dict(SUGGEST))
    monkeypatch.setattr(cli, "ask_claude", fake)
    assert cli.main(["confirm", "1"]) == 0
    out = capsys.readouterr().out
    assert out.index("What happened\n") < out.index("Suggested: wrong-result — it edited api.js")
    assert all(line.startswith(" " * 11) for line in out[out.index("Suggested:"):].split("\n\n")[0].splitlines()[1:])
    assert "  wasteful              got there but took far too long" in out
    assert out.endswith("Next\n  1. Record the suggested symptom:  culprit confirm 1 wrong-result\n"
                        "  2. Or say another one:  culprit confirm 1 <symptom>\n")
    assert read_symptom(runs, "s1:p1") is None and len(fake.calls) == 1
    assert "Claude did: 6 tool calls" in fake.calls[0][0]
    assert load_suggestion(runs, "s1:p1").reason == REASON
    assert stored_spend(episode_dir(runs, "s1:p1")) == CALL_CAP_USD


def test_a_cached_suggestion_is_not_paid_for_again(write_session, tmp_path, capsys, monkeypatch):
    queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(SUGGEST)))
    cli.main(["confirm", "1"])
    monkeypatch.setattr(cli, "ask_claude", FakeAsk())  # any call now fails the test
    assert cli.main(["confirm", "1"]) == 0
    assert "Suggested: wrong-result" in capsys.readouterr().out


def test_in_a_terminal_enter_accepts_the_suggestion(write_session, tmp_path, capsys, monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(SUGGEST), dict(DRAFT)))
    left, asked = terminal(monkeypatch, "", "skip")
    assert cli.main(["confirm", "1"]) == 0
    out = capsys.readouterr().out
    assert asked[0] == ("\nWhat went wrong? 1 stopped-short · 2 wrong-result · 3 ignored-instructions · 4 wasteful · "
                        "5 no-failure · 6 skip — Enter for wrong-result: ")
    assert "  1 stopped-short         quit before finishing\n" in out
    assert out.index("What happened") < out.index("Suggested:") < out.index("✓ Recorded what went wrong: wrong-result")
    assert read_symptom(runs, "s1:p1") is Symptom.WRONG_RESULT and left == []


def test_a_failed_suggestion_leaves_no_default_and_the_question_still_works(write_session, tmp_path, capsys,
                                                                           monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)

    def ask(prompt, schema):
        if "symptom" in schema["properties"]:
            raise ClaudeCallFailed("claude timed out after 600s")
        return dict(DRAFT)
    monkeypatch.setattr(cli, "ask_claude", ask)
    left, asked = terminal(monkeypatch, "", "4", "skip")  # Enter picks nothing without a suggestion
    assert cli.main(["confirm", "1"]) == 0
    captured = capsys.readouterr()
    assert "Suggested" not in captured.out and "Enter for" not in asked[0] and len(asked) == 3
    assert "no symptom suggested" in captured.err
    assert read_symptom(runs, "s1:p1") is Symptom.WASTEFUL and load_suggestion(runs, "s1:p1") is None


def test_an_explicit_symptom_is_recorded_beside_a_different_suggestion(write_session, tmp_path, capsys,
                                                                       monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(SUGGEST)))
    cli.main(["confirm", "1"])
    capsys.readouterr()
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(DRAFT)))  # the draft only: no new suggestion
    assert cli.main(["confirm", "1", "stopped-short"]) == 0
    out = capsys.readouterr().out
    assert out.index("Suggested: wrong-result") < out.index("✓ Recorded what went wrong: stopped-short")
    assert read_symptom(runs, "s1:p1") is Symptom.STOPPED_SHORT


def test_an_explicit_symptom_never_pays_for_a_suggestion(write_session, tmp_path, capsys, monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)
    fake = FakeAsk(dict(DRAFT))
    monkeypatch.setattr(cli, "ask_claude", fake)
    assert cli.main(["confirm", "1", "wasteful"]) == 0
    assert len(fake.calls) == 1 and "Suggested" not in capsys.readouterr().out


def test_the_walk_suggests_and_enter_accepts(write_session, tmp_path, capsys, monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(SUGGEST), dict(DRAFT)))
    left, asked = terminal(monkeypatch, "", "quit")
    assert cli.main(["confirm"]) == 0
    out = capsys.readouterr().out
    assert out.index("What happened") < out.index("Suggested: wrong-result") < out.index("✓ Recorded")
    assert asked[0].endswith("6 skip · 7 quit — Enter for wrong-result: ")
    assert read_symptom(runs, "s1:p1") is Symptom.WRONG_RESULT


def test_status_shows_a_cached_suggestion_without_a_call(write_session, tmp_path, capsys, monkeypatch):
    runs = queued_busy_episode(write_session, tmp_path)
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(SUGGEST)))
    cli.main(["confirm", "1"])
    capsys.readouterr()
    monkeypatch.setattr(cli, "ask_claude", FakeAsk())
    record_symptom(runs, "s1:p1", Symptom.WASTEFUL)
    assert cli.main(["status", "1"]) == 0
    out = capsys.readouterr().out
    assert out.index("What happened") < out.index("Suggested: wrong-result") < out.index("Progress")
