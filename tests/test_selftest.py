import json
from dataclasses import replace

from culprit.criterion import Criterion
from culprit.episodes import episodes_from_session
from culprit.replay import ReplaySettings
from culprit.selftest import (EXPECTED, IMPOSSIBLE, MISLEADING_INSTRUCTION_TEXT, PlantedFault, SelftestResult,
                              control_min_passes, is_control, plant, run_selftest, selftest_accuracy, selftest_dir)
from culprit.store import episode_dir
from culprit.verdict import Verdict, save_verdict
from records import human

CRITERION = Criterion("works", ("pytest -q",), (), approved=True)
BASE = ReplaySettings(prompt="fix the failing login test in the auth module please", model="claude-opus-5",
                      permission_mode="auto")


def test_each_fault_breaks_exactly_one_layer():
    assert plant(PlantedFault.TRUNCATED_PROMPT, BASE, CRITERION) == (
        replace(BASE, prompt="fix the failing login test in"), CRITERION)
    assert plant(PlantedFault.MISLEADING_INSTRUCTION, BASE, CRITERION) == (
        replace(BASE, append_system_prompt=MISLEADING_INSTRUCTION_TEXT), CRITERION)
    assert plant(PlantedFault.EDIT_TOOLS_REMOVED, BASE, CRITERION) == (replace(BASE, tools="Read,Grep,Glob"), CRITERION)
    assert plant(PlantedFault.WEAK_MODEL, BASE, CRITERION) == (replace(BASE, model="claude-haiku-5-5"), CRITERION)
    assert plant(PlantedFault.IMPOSSIBLE_CRITERION, BASE, CRITERION) == (BASE, IMPOSSIBLE)


def test_every_fault_has_its_expected_cause():
    assert {f.value: c.value for f, c in EXPECTED.items()} == {
        "truncated-prompt": "task", "misleading-instruction": "agent-setup", "edit-tools-removed": "harness",
        "weak-model": "model", "impossible-criterion": "unexplained"}
    assert IMPOSSIBLE.checks and all("false" in check for check in IMPOSSIBLE.checks)


def test_a_control_needs_four_in_five_of_the_n_baseline_runs_to_pass(tmp_path):
    assert [control_min_passes(n) for n in (5, 10, 7)] == [4, 8, 6]
    folder = episode_dir(tmp_path, "s1:p1") / "replays" / "baseline"
    folder.mkdir(parents=True)
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 7, "runs": 10, "cost_usd": 1}))
    assert is_control(tmp_path, "s1:p1", 10) is False
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 8, "runs": 10, "cost_usd": 1}))
    assert is_control(tmp_path, "s1:p1", 10) is True
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": 5, "runs": 5, "cost_usd": 1}))
    assert is_control(tmp_path, "s1:p1", 10) is False  # 5 of 5 runs is short of 8 of 10
    assert is_control(tmp_path, "s1:p1", 5) is True
    assert is_control(tmp_path, "s9:p9", 10) is False


def test_selftest_reports_each_fault_and_the_accuracy(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    runs = tmp_path / "runs"
    calls = []
    answers = {"truncated-prompt": "task", "misleading-instruction": "agent-setup", "edit-tools-removed": "harness",
               "weak-model": "task", "impossible-criterion": "unexplained"}

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        fault = kwargs["out_dir"].name
        calls.append((fault, base, criterion, kwargs["budget"].cap_usd))
        verdict = Verdict(episode.episode_id, symptom.value, answers[fault], "e", {}, {})
        save_verdict(kwargs["out_dir"], verdict)
        return verdict

    results = run_selftest(episode, CRITERION, BASE, runs_dir=runs, follow_ups=[], n=5, budget_per_fault=30.0,
                           diagnose_fn=fake_diagnose)
    assert [r.fault for r in results] == [f.value for f in PlantedFault]
    assert [r.correct for r in results] == [True, True, True, False, True]
    assert SelftestResult("weak-model", "model", "task").correct is False
    assert all(cap == 30.0 for *_, cap in calls)
    assert calls[3][1].model == "claude-haiku-5-5"
    assert selftest_dir(runs, "s1:p1", PlantedFault.WEAK_MODEL) == runs / "selftest" / "s1__p1" / "weak-model"
    assert selftest_accuracy(runs) == (4, 5)


def test_selftest_progress_names_the_planted_fault(write_session, tmp_path):
    from culprit.experiment import ReplayEvent
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path))]))
    events = []

    def fake_diagnose(episode, criterion, symptom, base, **kwargs):
        kwargs["progress"](ReplayEvent("start", "baseline", attempt=0, index=1, n=5))
        verdict = Verdict(episode.episode_id, symptom.value, "task", "e", {}, {})
        save_verdict(kwargs["out_dir"], verdict)
        return verdict

    run_selftest(episode, CRITERION, BASE, runs_dir=tmp_path / "runs", follow_ups=[], n=5, budget_per_fault=30.0,
                 faults=(PlantedFault.WEAK_MODEL,), diagnose_fn=fake_diagnose, progress=events.append)
    assert [e.scope for e in events] == ["weak-model"]
