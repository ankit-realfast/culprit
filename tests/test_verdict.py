from culprit.diagnose import Verdict, numbers_line


def outcome(name, passes, runs):
    return {"variant": name, "passes": passes, "runs": runs, "cost_usd": 1.0, "void": 0}


def test_a_single_cause_shows_the_baseline_the_winner_and_p():
    verdict = Verdict("a:1", "wrong-result", "harness", "e",
                      {"baseline": outcome("baseline", 0, 10), "task": outcome("task", 1, 10),
                       "harness": outcome("harness", 8, 10)}, {})
    assert numbers_line(verdict) == "harness: baseline 0/10 → 8/10, p = 0.0004"


def test_a_pair_shows_the_pair_that_passed():
    verdict = Verdict("a:1", "wrong-result", "multi-cause", "e",
                      {"baseline": outcome("baseline", 0, 5), "combined": outcome("combined", 5, 5),
                       "task+model": outcome("task+model", 4, 5)}, {}, ("task", "model"))
    assert numbers_line(verdict) == "multi-cause (task+model): baseline 0/5 → 4/5, p = 0.0238"


def test_multi_cause_without_a_pair_shows_every_change_together():
    verdict = Verdict("a:1", "wrong-result", "multi-cause", "e",
                      {"baseline": outcome("baseline", 0, 10), "combined": outcome("combined", 10, 10)}, {},
                      ("task", "harness", "model"))
    assert numbers_line(verdict) == "multi-cause (combined): baseline 0/10 → 10/10, p < 0.0001"


def test_a_verdict_without_a_winner_shows_the_baseline_only():
    verdict = Verdict("a:1", "wrong-result", "not-reproducible", "e", {"baseline": outcome("baseline", 7, 10)}, {})
    assert numbers_line(verdict) == "not-reproducible: baseline 7/10"


def test_a_verdict_without_replays_has_no_numbers():
    assert numbers_line(Verdict("a:1", "no-failure", "misjudged", "e", {}, {})) is None


def test_with_two_changes_a_losing_pair_gives_way_to_combined():
    verdict = Verdict("a:1", "wrong-result", "multi-cause", "e",
                      {"baseline": outcome("baseline", 0, 10), "combined": outcome("combined", 9, 10),
                       "task+model": outcome("task+model", 1, 10)}, {}, ("task", "model"))
    assert numbers_line(verdict).startswith("multi-cause (combined): baseline 0/10 → 9/10")
