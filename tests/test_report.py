from culprit.diagnose import Verdict, save_verdict
from culprit.report import collect, fix_list, grid, interactions, render_report


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
    assert [(f.cause, f.count) for f in fixes] == [("harness", 2), ("agent-setup", 1)]  # multi-cause: interactions
    assert fixes[0].episodes == ("a:1", "b:1")
    assert "tools" in fixes[0].action


def test_interacting_causes_are_listed_by_their_layers(tmp_path):
    sample(tmp_path)
    verdict(tmp_path, "f__1", "wrong-result", "multi-cause", pair=("model", "task"))
    verdict(tmp_path, "g__1", "wrong-result", "multi-cause", pair=("harness", "model"))
    assert [(i.layers, i.count, i.episodes) for i in interactions(collect(tmp_path))] == [
        ("task + model", 2, ("d:1", "f:1")), ("harness + model", 1, ("g:1",))]


def test_the_report_leads_with_what_to_fix_then_interactions_then_the_grid_and_trust(tmp_path):
    sample(tmp_path)
    text = render_report(collect(tmp_path), ["judge: 20 of 22 labels agree"])
    order = ["## What to fix first", "## Causes that interact", "## Symptom × cause", "## How far to trust this",
             "## Verdicts", "## Known limits"]
    assert [text.index(heading) for heading in order] == sorted(text.index(heading) for heading in order)
    fixes = text.split("## What to fix first")[1].split("##")[0]
    assert fixes.index("| harness | 2 |") < fixes.index("| agent-setup | 1 |") and "multi-cause" not in fixes
    assert "task + model" in text.split("## Causes that interact")[1].split("##")[0]


def test_when_only_interacting_causes_explain_failures_the_headline_names_the_pair(tmp_path):
    verdict(tmp_path, "a__1", "wrong-result", "multi-cause", pair=("task", "model"))
    text = render_report(collect(tmp_path), [])
    assert "**Change task + model together first:**" in text and "No single change to make" not in text


def test_the_report_leads_with_the_answer_and_states_the_limits(tmp_path):
    sample(tmp_path)
    text = render_report(collect(tmp_path), ["judge: 20 of 22 labels agree, kappa 0.81 — calibrated",
                                             "selftest: 4 of 5 planted faults named correctly"])
    first_lines = text.splitlines()[:5]
    assert any("harness" in line and "2 of 5" in line for line in first_lines)
    for expected in ["| Fix", "| Symptom", "wrong-result", "kappa 0.81", "4 of 5 planted faults",
                     "Known limits", "project's memory", "no environment fix supplied", "harness passed 4/5"]:
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


def test_each_verdict_states_how_far_its_restore_can_be_trusted(tmp_path):
    import json
    verdict(tmp_path, "a__1", "wrong-result", "harness")
    (tmp_path / "a__1" / "restore.json").write_text(json.dumps(
        {"snapshot_agreed": 3, "checked": 4, "restored": 9, "unknown": 2, "missing_backups": 1, "outside_cwd": 0,
         "set_from_episode": 4, "removed": 0}))
    text = render_report(collect(tmp_path), [])
    assert "restore: snapshot agreed with the episode's record for 3 of 4 files; 2 unknown, 1 missing" in text
    for limit in ["yours or Claude Code's", "start of the episode"]:
        assert limit in text


def test_each_verdict_shows_the_numbers_behind_it(tmp_path):
    outcomes = {"baseline": {"variant": "baseline", "passes": 0, "runs": 10, "cost_usd": 1.0, "void": 0},
                "harness": {"variant": "harness", "passes": 8, "runs": 10, "cost_usd": 1.0, "void": 0}}
    save_verdict(tmp_path / "a__1", Verdict("a:1", "wrong-result", "harness", "e", outcomes, {}))
    assert "harness: baseline 0/10 → 8/10, p = 0.0004" in render_report(collect(tmp_path), [])


def test_an_agent_setup_verdict_shows_the_tested_lines_and_the_untested_placement(tmp_path):
    import json
    verdict(tmp_path, "c__1", "ignored-instructions", "agent-setup")
    (tmp_path / "c__1" / "variants").mkdir()
    (tmp_path / "c__1" / "variants" / "agent-setup.json").write_text(json.dumps({
        "prompt": "p", "model": None, "permission_mode": "auto", "claude_md_addition": "Run the tests.\nSay what failed.",
        "draft": {"placement": "CLAUDE.local.md", "reason": "personal habit", "written_to": "/work/proj/CLAUDE.md"}}))
    text = render_report(collect(tmp_path), []).split("## Verdicts")[1]
    assert "adding these lines to /work/proj/CLAUDE.md made it pass" in text
    assert "Run the tests." in text and "Say what failed." in text
    assert "suggested placement (not tested): CLAUDE.local.md — personal habit" in text


def test_other_verdicts_show_no_placement(tmp_path):
    sample(tmp_path)
    assert "suggested placement" not in render_report(collect(tmp_path), [])


def test_each_verdict_says_how_many_runs_stopped_at_a_limit(tmp_path):
    outcomes = {"baseline": {"variant": "baseline", "passes": 0, "runs": 10, "cost_usd": 1.0, "void": 0,
                             "limits": {"budget": 6, "time": 1}},
                "harness": {"variant": "harness", "passes": 8, "runs": 10, "cost_usd": 1.0, "void": 0}}
    save_verdict(tmp_path / "a__1", Verdict("a:1", "wrong-result", "harness", "e", outcomes, {}))
    assert "stopped at a limit: baseline 7 of 10 (budget 6, time 1)" in render_report(collect(tmp_path), [])


def test_each_verdict_shows_what_each_variant_cost_per_run(tmp_path):
    tokens = {"input_tokens": 10_000, "output_tokens": 20_000}
    outcomes = {"baseline": {"variant": "baseline", "passes": 0, "runs": 10, "cost_usd": 3.0, "void": 0,
                             "tokens": tokens},
                "harness": {"variant": "harness", "passes": 8, "runs": 10, "cost_usd": 4.1, "void": 0,
                            "tokens": tokens}}
    save_verdict(tmp_path / "a__1", Verdict("a:1", "wrong-result", "harness", "e", outcomes, {}))
    text = render_report(collect(tmp_path), [])
    assert "p = 0.0004 · $0.41/run, 3k tokens/run" in text
    assert "cost per run: baseline $0.30, 3k tokens (1k in, 2k out, 0 cached); harness $0.41" in text


def test_each_verdict_says_which_model_each_variant_ran_as(tmp_path):
    outcomes = {"baseline": {"variant": "baseline", "passes": 0, "runs": 10, "cost_usd": 1.0, "void": 0,
                             "models": {"claude-opus-5": 10}},
                "harness": {"variant": "harness", "passes": 0, "runs": 10, "cost_usd": 1.0, "void": 0,
                            "models": {"claude-opus-5": 10}},
                "model": {"variant": "model", "passes": 9, "runs": 10, "cost_usd": 1.0, "void": 0,
                          "models": {"claude-fable-5-1": 10}},
                "original-max-effort": {"variant": "original-max-effort", "passes": 2, "runs": 10, "cost_usd": 1.0,
                                        "void": 0, "models": {"claude-opus-5": 9, "claude-sonnet-5-5": 1}}}
    save_verdict(tmp_path / "a__1", Verdict("a:1", "wrong-result", "model", "e", outcomes, {}))
    text = render_report(collect(tmp_path), [])
    assert ("models: baseline ran as claude-opus-5; model test: Fable at max effort, ran as claude-fable-5-1; "
            "original model at max effort, ran as claude-opus-5 (9), claude-sonnet-5-5 (1)") in text
