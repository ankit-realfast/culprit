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

