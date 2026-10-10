from culprit.report import collect, fix_list, grid, render_report
from culprit.verdict import Verdict, save_verdict


def verdict(runs, episode_folder, symptom, cause, pair=(), untested=None):
    episode_id = episode_folder.replace("__", ":")
    save_verdict(runs / episode_folder, Verdict(episode_id, symptom, cause, f"{cause} passed 4/5 against the baseline's 0/5",
                                                {}, untested or {}, pair))


def sample(runs):
    verdict(runs, "a__1", "wrong-result", "harness")
    verdict(runs, "b__1", "wrong-result", "harness")
    verdict(runs, "c__1", "ignored-instructions", "agent-setup", untested={"environment": "no environment fix supplied"})
    verdict(runs, "d__1", "stopped-short", "multi-cause", pair=("task", "model"))
    verdict(runs, "e__1", "no-failure", "misjudged")
    save_verdict(runs / "selftest" / "a__1" / "weak-model", Verdict("a:1", "wrong-result", "model", "e", {}, {}))


def test_collect_reads_real_verdicts_and_skips_selftest(tmp_path):
    sample(tmp_path)
    assert [row.episode_id for row in collect(tmp_path)] == ["a:1", "b:1", "c:1", "d:1", "e:1"]


def test_grid_counts_symptom_by_cause(tmp_path):
    sample(tmp_path)
    cells = grid(collect(tmp_path))
    assert cells[("wrong-result", "harness")] == 2
    assert cells[("no-failure", "misjudged")] == 1
    assert sum(cells.values()) == 5


def test_fixes_rank_by_count_then_cause_order_and_skip_non_fixes(tmp_path):
    sample(tmp_path)
    fixes = fix_list(collect(tmp_path))
    assert [(f.cause, f.count) for f in fixes] == [("harness", 2), ("agent-setup", 1), ("multi-cause", 1)]
    assert fixes[0].episodes == ("a:1", "b:1")
    assert "tools" in fixes[0].action


def test_multi_cause_names_its_layers_in_the_fix_list(tmp_path):
    sample(tmp_path)
    multi = next(f for f in fix_list(collect(tmp_path)) if f.cause == "multi-cause")
    assert "task + model" in multi.action


def test_the_report_leads_with_the_answer_and_states_the_limits(tmp_path):
    sample(tmp_path)
    text = render_report(collect(tmp_path), ["judge: 20 of 22 labels agree, kappa 0.81 — calibrated",
                                             "selftest: 4 of 5 planted faults named correctly"])
    first_lines = text.splitlines()[:4]
    assert any("harness" in line and "2 of 5" in line for line in first_lines)
    for expected in ["| Fix", "| Symptom", "wrong-result", "kappa 0.81", "4 of 5 planted faults",
                     "Known limits", "auto-memory", "no environment fix supplied", "harness passed 4/5"]:
        assert expected in text


def test_a_report_with_no_verdicts_says_what_to_run(tmp_path):
    text = render_report(collect(tmp_path), ["judge: 0 of 0 labels agree", "selftest: not run"])
    assert "No verdicts yet" in text and "culprit diagnose" in text
    assert "| Symptom" not in text


def test_unexplained_is_never_the_headline_fix_and_is_listed_for_review(tmp_path):
    verdict(tmp_path, "a__1", "wrong-result", "unexplained")
    verdict(tmp_path, "b__1", "wrong-result", "unexplained")
    verdict(tmp_path, "c__1", "wrong-result", "model")
    rows = collect(tmp_path)
    assert [f.cause for f in fix_list(rows)] == ["model"]
    text = render_report(rows, [])
    assert "**Change model first:**" in text
    assert "Needs your review" in text and "a:1" in text.split("Needs your review")[1]


def test_a_report_with_only_unexplained_verdicts_makes_no_fix_claim(tmp_path):
    verdict(tmp_path, "a__1", "wrong-result", "unexplained")
    text = render_report(collect(tmp_path), [])
    assert "Change unexplained" not in text
