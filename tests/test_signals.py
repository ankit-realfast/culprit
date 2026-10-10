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
