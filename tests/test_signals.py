from culprit.logs import FailureSignals, episodes_from_session, signals_for
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



def test_an_episodes_cost_is_the_running_total_it_ends_on_less_the_one_before_it(write_session):
    from culprit.logs import episode_cost_usd
    from records import cost_state
    first, second, third = episodes_from_session(write_session([
        human("a", "p1"), assistant(), cost_state(1.5),
        human("b", "p2"), assistant(), cost_state(4.0),
        human("c", "p3"), assistant()]))
    assert episode_cost_usd(first) == 1.5  # a session's first episode starts from nothing
    assert episode_cost_usd(second) == 2.5
    assert episode_cost_usd(third) is None  # no running total recorded after it


def test_a_follow_up_with_no_earlier_running_total_has_no_known_cost(write_session):
    from culprit.logs import episode_cost_usd
    from records import cost_state
    _, second = episodes_from_session(write_session([human("a", "p1"), assistant(),
                                                     human("b", "p2"), assistant(), cost_state(4.0)]))
    assert episode_cost_usd(second) is None  # 4.0 includes the first episode's spend
