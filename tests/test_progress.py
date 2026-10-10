import io

from culprit.diagnose import ReplayEvent
from culprit.progress import Heartbeat, ProgressPrinter, elapsed


class FakeHeartbeat:
    made = []

    def __init__(self, beat, interval):
        self.beat, self.interval, self.stopped = beat, interval, False
        FakeHeartbeat.made.append(self)

    def stop(self):
        self.stopped = True


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def start(index=2):
    return ReplayEvent("start", "baseline", attempt=index - 1, index=index, n=5)


def finish(index=2):
    return ReplayEvent("finish", "baseline", attempt=index - 1, index=index, n=5, result="pass", cost_usd=1.0,
                       spent_usd=2.0)


def test_the_printer_beats_while_a_replay_runs_and_stops_when_it_finishes():
    FakeHeartbeat.made.clear()
    out, clock = io.StringIO(), Clock()
    printer = ProgressPrinter(out, interval=30, clock=clock, heartbeat=FakeHeartbeat)
    printer(start())
    (beat,) = FakeHeartbeat.made
    assert beat.interval == 30
    clock.now += 150
    beat.beat()
    assert out.getvalue() == "… still running baseline #2, 2m30s\n"
    printer(finish())
    assert beat.stopped


def test_a_new_start_or_closing_the_printer_stops_a_heartbeat_left_running():
    FakeHeartbeat.made.clear()
    with ProgressPrinter(io.StringIO(), clock=Clock(), heartbeat=FakeHeartbeat) as printer:
        printer(start(1))  # its replay raised, so no finish came
        printer(start(2))
        first, second = FakeHeartbeat.made
        assert first.stopped and not second.stopped
    assert second.stopped


def test_elapsed_reads_as_minutes_and_seconds():
    assert elapsed(45) == "45s"
    assert elapsed(150) == "2m30s"
    assert elapsed(3725) == "1h02m05s"


def test_the_heartbeat_thread_beats_each_interval_until_the_wait_says_stop():
    beats, waits = [], [False, False, True]

    def wait(interval):
        assert interval == 30
        return waits.pop(0)

    heartbeat = Heartbeat(lambda: beats.append(1), 30, wait=wait)
    heartbeat.stop()
    assert beats == [1, 1]


def test_stopping_the_real_heartbeat_wakes_it_at_once():
    beats = []
    heartbeat = Heartbeat(lambda: beats.append(1), 3600)
    heartbeat.stop()  # would hang for an hour if the wait were a sleep
    assert beats == []
