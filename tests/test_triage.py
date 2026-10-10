from culprit.signals import FailureSignals
from culprit.triage import EpisodeSummary, excerpt, percentile, rank_candidates, score

CLEAN = FailureSignals(False, 0, 0, 0, 10.0)


def summary(episode_id, signals):
    return EpisodeSummary(episode_id, "/s.jsonl", "prompt", signals)


def test_excerpt_is_one_line_and_bounded():
    assert excerpt("fix\n  the   bug") == "fix the bug"
    assert excerpt("x" * 100, limit=10) == "x" * 9 + "…"


def test_percentile_uses_nearest_rank_and_survives_no_data():
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 0.9) == 9.0
    assert percentile([], 0.9) == float("inf")


def test_score_weights_each_signal():
    assert score(CLEAN, slow_threshold_s=60) == 0
    assert score(FailureSignals(True, 0, 0, 0, 0.0), 60) == 5
    assert score(FailureSignals(False, 9, 0, 0, 0.0), 60) == 5  # tool errors capped
    assert score(FailureSignals(False, 0, 2, 1, 0.0), 60) == 2  # API errors do not count
    assert score(FailureSignals(False, 0, 0, 0, 61.0), 60) == 3


def test_rank_orders_by_score_drops_clean_episodes_and_keeps_top():
    summaries = [
        summary("clean", CLEAN),
        summary("interrupted", FailureSignals(True, 0, 0, 0, 10.0)),
        summary("errored", FailureSignals(False, 2, 0, 0, 10.0)),
        summary("both", FailureSignals(True, 2, 0, 0, 10.0)),
    ]
    ranked = rank_candidates(summaries, top=2)
    assert [c.summary.episode_id for c in ranked] == ["both", "interrupted"]
    assert [c.score for c in ranked] == [7, 5]


def test_an_episode_whose_only_signal_is_api_errors_is_not_a_candidate():
    ranked = rank_candidates([summary("api", FailureSignals(False, 0, 3, 0, 10.0)), summary("clean", CLEAN)], top=5)
    assert ranked == []


def test_a_correction_outweighs_every_other_signal_combined():
    corrected = FailureSignals(False, 0, 0, 0, 0.0, corrected="no, still broken")
    everything_else = FailureSignals(True, 99, 9, 99, 999.0)
    assert score(corrected, 60) > score(everything_else, 60)
    ranked = rank_candidates([summary("noisy", everything_else), summary("corrected", corrected)], top=5)
    assert [c.summary.episode_id for c in ranked] == ["corrected", "noisy"]


def test_other_signals_break_ties_between_corrected_episodes():
    plain = FailureSignals(False, 0, 0, 0, 0.0, corrected="undo that")
    interrupted = FailureSignals(True, 0, 0, 0, 0.0, corrected="undo that")
    ranked = rank_candidates([summary("plain", plain), summary("interrupted", interrupted)], top=5)
    assert [c.summary.episode_id for c in ranked] == ["interrupted", "plain"]


def test_summaries_read_each_episodes_follow_up_from_the_next_prompt(write_session):
    from culprit.triage import summarize_session
    from records import assistant, human
    path = write_session([human("fix the login page", "p1"), assistant(),
                          human("no, still broken", "p2"), assistant(),
                          human("thanks, now add a test", "p3"), assistant()])
    assert [s.signals.corrected for s in summarize_session(path)] == ["no, still broken", "", ""]
