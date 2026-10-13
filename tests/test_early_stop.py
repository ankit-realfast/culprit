"""Stopping a replay once it breaks the criterion's limits, and a variant once its result can no longer change."""
import json
import subprocess
import sys
import threading
import time

import pytest

from culprit.diagnose import (EpisodeBudget, VariantOutcome, bounded_by, beats, run_variant, settled_against,
                              settled_not_reproducible)
from culprit.logs import episodes_from_session
from culprit.model import Criterion, Judgement, Limits, judge_replay
from culprit.replay import (LIMIT_TIME, LIMIT_TOOL_CALLS, ReplayResult, ReplaySettings, StoppedEarly,
                            run_in_own_process_group, run_replay, tool_call_counter)
from fakes import FakeRun
from records import human

STOPPED = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                      "total_cost_usd": 0.8, "num_turns": 16, "duration_ms": 70_000})


def tool_use_line(name="Bash", parent=None):
    return json.dumps({"type": "assistant", "parent_tool_use_id": parent,
                       "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}})


def settings(**overrides):
    return ReplaySettings(**{"prompt": "fix", "model": "claude-opus-5", "permission_mode": "auto", **overrides})


def pristine_tree(tmp_path):
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "a.py").write_text("old\n")
    return pristine


# --- stopping one replay ---

def test_a_command_watched_line_by_line_is_stopped_when_a_line_says_so():
    script = "import time\nfor i in range(50):\n    print(f'line{i}', flush=True)\n    time.sleep(0.1)"
    began = time.monotonic()
    with pytest.raises(StoppedEarly) as stopped:
        run_in_own_process_group([sys.executable, "-c", script],
                                 stop_when=lambda line: "too many" if line.startswith("line2") else None)
    assert stopped.value.reason == "too many"
    assert "line2" in stopped.value.stdout and "line40" not in stopped.value.stdout
    assert time.monotonic() - began < 5


def test_a_stop_asks_politely_first_so_claude_can_report_what_it_spent():
    script = ("import signal, sys, time\n"
              "signal.signal(signal.SIGINT, lambda *a: (print('cost report', flush=True), sys.exit(0)))\n"
              "print('working', flush=True)\ntime.sleep(30)")
    with pytest.raises(subprocess.TimeoutExpired) as timed_out:
        run_in_own_process_group([sys.executable, "-c", script], timeout=1)
    assert "cost report" in timed_out.value.stdout
    with pytest.raises(StoppedEarly) as stopped:
        run_in_own_process_group([sys.executable, "-c", script], stop_when=lambda line: "now" if "working" in line else None)
    assert "cost report" in stopped.value.stdout


def test_a_cancelled_command_stops_as_abandoned():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(StoppedEarly) as stopped:
        run_in_own_process_group([sys.executable, "-c", "import time; time.sleep(30)"], stop_when=lambda l: None,
                                 cancel=cancel)
    assert stopped.value.reason == "abandoned"


def test_the_counter_stops_at_the_first_own_tool_call_over_the_limit():
    count = tool_call_counter(2)
    assert [count(line) for line in (tool_use_line(), tool_use_line(parent="t1"), tool_use_line(), "not json",
                                     tool_use_line())] == [None, None, None, None, LIMIT_TOOL_CALLS]


def test_a_replay_stopped_over_its_tool_calls_counts_at_its_real_cost_and_fails_without_a_model(tmp_path):
    stdout = "\n".join([tool_use_line()] * 3 + [STOPPED]) + "\n"
    run = FakeRun(claude_raises=StoppedEarly(LIMIT_TOOL_CALLS, stdout, ""))
    result = run_replay(settings(max_budget_usd=10.0), pristine_tree(tmp_path), tmp_path / "run0",
                        tmp_path / ".culprit-replay-s", (), run=run, projects_dir=tmp_path / "p", max_tool_calls=2)
    claude_kwargs = next(kwargs for command, kwargs in run.calls if command[0] == "claude")
    assert claude_kwargs["stop_when"] is not None
    assert (result.limit, result.void_reason, result.cost_usd, result.duration_s) == (LIMIT_TOOL_CALLS, None, 0.8, 70)
    judgement = judge_replay(Criterion("works", (), (), approved=True, limits=Limits(max_tool_calls=2)), result,
                             ask=lambda *a: pytest.fail("no model call"))
    assert (judgement.passed, judgement.by, judgement.reason) == (False, "limit", "over the limit: 3 tool calls > 2")


def test_a_replay_stopped_at_its_time_limit_is_charged_what_claude_reported(tmp_path):
    run = FakeRun(claude_raises=subprocess.TimeoutExpired("claude", 1, output=STOPPED + "\n"))
    result = run_replay(settings(max_budget_usd=10.0, timeout_s=245), pristine_tree(tmp_path), tmp_path / "run0",
                        tmp_path / ".culprit-replay-t", (), run=run, projects_dir=tmp_path / "p")
    assert (result.limit, result.void_reason, result.cost_usd, result.duration_s) == (LIMIT_TIME, None, 0.8, 245)


def test_an_abandoned_replay_keeps_its_cost_and_is_marked_abandoned(tmp_path):
    run = FakeRun(claude_raises=StoppedEarly("abandoned", STOPPED + "\n", ""))
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-a", (),
                        run=run, projects_dir=tmp_path / "p", cancel=threading.Event())
    assert result.abandoned and result.cost_usd == 0.8
    assert not (tmp_path / ".culprit-replay-a").exists()


def test_the_criterions_limits_cap_the_replays_time_and_spend():
    limits = Limits(max_tool_calls=15, max_minutes=4, max_usd=3.0)
    bounded = bounded_by(settings(timeout_s=1800, max_budget_usd=10.0), limits)
    assert (bounded.timeout_s, bounded.max_budget_usd) == (245, 3.0)  # 5 s past the limit, so the run is over it
    tighter = bounded_by(settings(timeout_s=200, max_budget_usd=2.0), limits)
    assert (tighter.timeout_s, tighter.max_budget_usd) == (200, 2.0)
    assert bounded_by(settings(timeout_s=1800), None).timeout_s == 1800


# --- stopping a variant ---

def test_the_baseline_settles_only_once_it_cannot_reproduce():
    settled = settled_not_reproducible(10)
    assert settled(6, 6) and settled(6, 9)
    assert not settled(5, 9) and not settled(0, 5)  # reproducible: every later variant needs its full count


def test_a_variant_settles_once_no_remaining_result_can_change_whether_it_beats_the_baseline():
    baseline = VariantOutcome("baseline", 4, 10, 0.0)
    settled = settled_against(baseline, 10)
    assert settled(0, 2)  # two fails: even 8 more passes (8/10 vs 4/10) would not beat it
    assert not settled(1, 1)
    assert settled(9, 9)  # 9/10 beats 4/10 whatever the last run does


def test_a_settled_variant_stops_the_runs_still_going_and_counts_only_the_judged_ones(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path / "proj"))]))
    criterion = Criterion("works", (), (), approved=True)

    def replay(settings, pristine, run_dir, workdir, checks, cancel=None, **kwargs):
        run_dir.mkdir(parents=True, exist_ok=True)
        if int(run_dir.name) < 2:
            return ReplayResult(run_dir, 0, False, "done", 1.0, 1, ())
        cancel.wait(5)
        return ReplayResult(run_dir, -1, True, "", 0.3, 1, (), abandoned=True)

    budget = EpisodeBudget(100.0)
    outcome = run_variant("task", settings(max_budget_usd=1.0), n=10, episode=episode, runs_dir=tmp_path / "runs",
                          criterion=criterion, budget=budget, replay=replay,
                          judge=lambda c, r: Judgement(False, "r", "model"), parallel=10,
                          settled=settled_against(VariantOutcome("baseline", 4, 10, 0.0), 10))
    assert (outcome.passes, outcome.runs, outcome.void) == (0, 2, 0)
    assert outcome.cost_usd == round(2 * 1.0 + 8 * 0.3, 4)  # what the stopped runs spent still counts
    assert not beats(outcome, VariantOutcome("baseline", 4, 10, 0.0))


def test_progress_says_a_stopped_run_was_not_needed():
    import io
    from culprit.cli import ProgressPrinter
    from culprit.diagnose import ReplayEvent
    out = io.StringIO()
    ProgressPrinter(out)(ReplayEvent("finish", "task", attempt=3, index=4, n=10, result="abandoned", cost_usd=0.3,
                                     spent_usd=9.0))
    assert "task #4 of 10: stopped, not needed (the result was already settled), $0.30" in out.getvalue()
