import io
import os
import signal
import threading
import time

import pytest

from culprit.cli import ProgressPrinter
from culprit.diagnose import (DEFAULT_PARALLEL, BudgetExhausted, EpisodeBudget, ReplayEvent, parallel_replays,
                              run_variant)
from culprit.logs import episodes_from_session
from culprit.model import Criterion, Judgement
from culprit.replay import ReplayResult, ReplaySettings, run_in_own_process_group, stop_replays
from culprit.store import episode_dir
from records import human

CRITERION = Criterion("works", (), (), approved=True)
SETTINGS = ReplaySettings(prompt="fix", model="claude-opus-5", permission_mode="auto", max_budget_usd=1.0)


def episode_at(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path / "proj"))]))
    return episode


def passes_on_even_attempts(criterion, result):
    return Judgement(int(result.run_dir.name) % 2 == 0, "r", "model")


def test_a_variants_replays_run_at_the_same_time(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    together = threading.Barrier(3, timeout=5)  # breaks unless all three replays are running at once

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        together.wait()
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", 0.25, 1, ())

    outcome = run_variant("baseline", SETTINGS, n=3, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                          budget=EpisodeBudget(10.0), replay=replay, judge=passes_on_even_attempts, parallel=3)
    assert (outcome.passes, outcome.runs, outcome.cost_usd) == (2, 3, 0.75)


def test_each_judgement_is_stored_with_its_own_run(write_session, tmp_path):
    import json
    episode = episode_at(write_session, tmp_path)

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        time.sleep(0.01 * (5 - int(run_dir.name)))  # later attempts finish first
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", 0.1, 1, ())

    runs = tmp_path / "runs"
    run_variant("baseline", SETTINGS, n=5, episode=episode, runs_dir=runs, criterion=CRITERION,
                budget=EpisodeBudget(10.0), replay=replay, judge=passes_on_even_attempts, parallel=5)
    folder = episode_dir(runs, episode.episode_id) / "replays" / "baseline"
    assert [json.loads((folder / str(i) / "judgement.json").read_text())["passed"] for i in range(5)] == \
        [True, False, True, False, True]


def test_runs_in_flight_never_reserve_more_than_the_budget_holds(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    lock, running, peak, calls = threading.Lock(), [0], [0], []

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            calls.append(run_dir)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", 1.0, 1, ())

    with pytest.raises(BudgetExhausted):
        run_variant("baseline", SETTINGS, n=5, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                    budget=EpisodeBudget(cap_usd=3.2), replay=replay, judge=passes_on_even_attempts, parallel=5)
    # each run reserves its $1.00 cap plus the $0.50 judge cap: only two fit in $3.20
    assert (peak[0], len(calls)) == (2, 2)


def test_a_failing_replay_lets_the_others_finish_and_keep_their_judgements(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        if run_dir.name == "0":
            raise RuntimeError("setup failed")
        time.sleep(0.05)
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", 0.1, 1, ())

    runs = tmp_path / "runs"
    with pytest.raises(RuntimeError, match="setup failed"):
        run_variant("baseline", SETTINGS, n=3, episode=episode, runs_dir=runs, criterion=CRITERION,
                    budget=EpisodeBudget(10.0), replay=replay, judge=passes_on_even_attempts, parallel=3)
    folder = episode_dir(runs, episode.episode_id) / "replays" / "baseline"
    assert (folder / "1" / "judgement.json").is_file() and (folder / "2" / "judgement.json").is_file()


def test_ctrl_c_kills_every_running_replay_and_stores_no_judgement(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    started = threading.Barrier(3, timeout=5)

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        run_dir.mkdir(parents=True, exist_ok=True)
        if started.wait() == 0:
            threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT)).start()  # Ctrl-C once all three run
        run_in_own_process_group(["sleep", "30"])
        return ReplayResult(run_dir, 0, False, "done", 0.1, 1, ())

    began = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_variant("baseline", SETTINGS, n=3, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                    budget=EpisodeBudget(10.0), replay=replay, judge=passes_on_even_attempts, parallel=3)
    assert time.monotonic() - began < 10
    folder = episode_dir(tmp_path / "runs", episode.episode_id) / "replays" / "baseline"
    assert not list(folder.glob("*/judgement.json"))


def test_after_a_stop_the_next_command_in_the_same_process_runs_again():
    stop_replays()
    with pytest.raises(KeyboardInterrupt):
        run_in_own_process_group(["true"])
    from culprit.replay import allow_replays
    allow_replays()
    assert run_in_own_process_group(["true"]).returncode == 0


def test_how_many_replays_run_at_once_comes_from_culprit_parallel(monkeypatch):
    monkeypatch.delenv("CULPRIT_PARALLEL", raising=False)
    assert parallel_replays() == DEFAULT_PARALLEL == 10
    monkeypatch.setenv("CULPRIT_PARALLEL", "3")
    assert parallel_replays() == 3
    monkeypatch.setenv("CULPRIT_PARALLEL", "0")
    assert parallel_replays() == 1


class FakeHeartbeat:
    made = []

    def __init__(self, beat, interval):
        self.beat, self.interval, self.stopped = beat, interval, False
        FakeHeartbeat.made.append(self)

    def stop(self):
        self.stopped = True


def test_one_heartbeat_names_every_replay_still_running():
    FakeHeartbeat.made.clear()
    out, now = io.StringIO(), [100.0]
    printer = ProgressPrinter(out, interval=30, clock=lambda: now[0], heartbeat=FakeHeartbeat)
    printer(ReplayEvent("start", "baseline", attempt=0, index=1, n=3))
    now[0] += 10
    printer(ReplayEvent("start", "baseline", attempt=1, index=2, n=3))
    (beat,) = FakeHeartbeat.made
    now[0] += 140
    beat.beat()
    assert out.getvalue() == "… still running baseline #1, #2, 2m30s\n"
    printer(ReplayEvent("finish", "baseline", attempt=0, index=1, n=3, result="pass", cost_usd=1.0, spent_usd=2.0))
    assert not beat.stopped
    beat.beat()
    assert out.getvalue().endswith("… still running baseline #2, 2m20s\n")  # timed from the oldest still running
    printer(ReplayEvent("finish", "baseline", attempt=1, index=2, n=3, result="fail", cost_usd=1.0, spent_usd=3.0))
    assert beat.stopped
