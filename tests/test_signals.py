from culprit.episodes import episodes_from_session
from culprit.signals import FailureSignals, signals_for
from records import assistant, human, interrupt, system, tool_result


def only_episode(write_session, records):
    (episode,) = episodes_from_session(write_session(records))
    return episode


def test_a_clean_episode_has_no_signals(write_session):
    episode = only_episode(write_session, [human("x", "p1"), assistant(), tool_result("p1")])
    assert signals_for(episode) == FailureSignals(False, 0, 0, 0, 0.0)


def test_an_interrupt_is_counted(write_session):
    episode = only_episode(write_session, [human("x", "p1"), assistant(), interrupt("p1")])
    assert signals_for(episode).interrupted is True


def test_errors_compactions_and_duration_are_counted(write_session):
    episode = only_episode(write_session, [
        human("x", "p1"),
        tool_result("p1", is_error=True), tool_result("p1", is_error=True), tool_result("p1"),
        system("api_error"), system("compact_boundary"),
        system("turn_duration", durationMs=30_000), system("turn_duration", durationMs=12_500),
    ])
    assert signals_for(episode) == FailureSignals(False, 2, 1, 1, 42.5)


def test_an_interrupted_episode_without_turn_duration_is_timed_by_its_timestamps(write_session):
    # Claude Code writes no turn_duration record for a turn the human interrupted.
    later = assistant()
    later["timestamp"] = "2026-10-01T10:01:30.500Z"
    stopped = interrupt("p1")
    stopped["timestamp"] = "2026-10-01T10:02:05Z"
    episode = only_episode(write_session, [human("x", "p1"), later, tool_result("p1", is_error=True), stopped])
    assert signals_for(episode).duration_s == 125.0


def test_turn_duration_records_win_over_timestamps(write_session):
    later = assistant()
    later["timestamp"] = "2026-10-01T10:30:00Z"
    episode = only_episode(write_session, [human("x", "p1"), later, system("turn_duration", durationMs=4_000)])
    assert signals_for(episode).duration_s == 4.0


def test_unparseable_timestamps_leave_duration_zero(write_session):
    odd = assistant()
    odd["timestamp"] = "not a time"
    episode = only_episode(write_session, [human("x", "p1"), odd, interrupt("p1")])
    assert signals_for(episode).duration_s == 0.0


CORRECTIONS = [
    "no, still broken",
    "No. That is not what I asked for",
    "nope",
    "no i meant the other file",
    "no - use the helper",
    "wrong file",
    "why are we creating a second config file?",
    "why did you delete the tests",
    "that's wrong, the total should be 12",
    "That’s not right",
    "you didn't update the docs",
    "You forgot the migration",
    "undo that",
    "revert the last change",
    "stop",
    "Stop, this is going the wrong way",
    "it still doesn't work",
    "still failing with the same error",
    "I told you to use the existing helper",
    "not what I wanted",
]

NOT_CORRECTIONS = [
    "thanks, now add a test for the parser",
    "no worries, next let's do the README",
    "no problem — can you also rename the module?",
    "no reviewer needed for this one",
    "no tests for the scripts folder yet?",
    "now make the button blue",
    "why is the build slow on CI?",
    "nothing else, commit it",
    "notes: the API returns 404 for missing ids",
    "stopwatch should count down instead",
    "great, what's next",
    "",
]


def test_follow_ups_that_correct_claude_are_recognised():
    from culprit.signals import is_correction
    assert [text for text in CORRECTIONS if not is_correction(text)] == []


def test_ordinary_follow_ups_are_not_taken_for_corrections():
    from culprit.signals import is_correction
    assert [text for text in NOT_CORRECTIONS if is_correction(text)] == []


def test_a_correcting_follow_up_is_kept_as_a_short_excerpt(write_session):
    episode = only_episode(write_session, [human("x", "p1"), assistant()])
    assert signals_for(episode, follow_up="thanks").corrected == ""
    assert signals_for(episode).corrected == ""
    long = "no, still broken — " + "the page renders blank after login " * 4
    corrected = signals_for(episode, follow_up=long).corrected
    assert corrected.startswith("no, still broken") and len(corrected) <= 40
