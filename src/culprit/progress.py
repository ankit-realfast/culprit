"""What the CLI shows while replays run: one stderr line per finished replay, and a heartbeat between."""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, TextIO

from culprit.experiment import ReplayEvent


HEARTBEAT_S = 30


class Heartbeat:
    """Calls beat() every interval seconds on a daemon thread until stop().

    `wait(interval)` returns True to stop; the default waits on an event, so stop() wakes it at once.
    """

    def __init__(self, beat: Callable[[], None], interval: float,
                 wait: Callable[[float], bool] | None = None):
        self._stopped = threading.Event()
        self._beat, self._interval = beat, interval
        self._wait = wait or self._stopped.wait
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._wait(self._interval):
            self._beat()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()


def elapsed(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def describe(event: ReplayEvent) -> str:
    """`baseline #2 of 5` — prefixed with the scope (planted fault, episode) when there is one."""
    name = f"{event.variant} #{event.index} of {event.n}"
    return f"{event.scope}: {name}" if event.scope else name


class ProgressPrinter:
    """The progress callback the CLI hands to run_variant, diagnose, run_selftest and diagnose_all.

    Use it as a context manager so a heartbeat never outlives the command.
    """

    def __init__(self, stream: TextIO | None = None, *, interval: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.monotonic, heartbeat=Heartbeat):
        self.stream = stream or sys.stderr
        self.interval, self.clock, self.heartbeat = interval, clock, heartbeat
        self._running = None

    def __enter__(self) -> ProgressPrinter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._running is not None:
            self._running.stop()
            self._running = None

    def __call__(self, event: ReplayEvent) -> None:
        self.close()  # a replay that raised never sent its finish
        if event.kind == "start":
            started = self.clock()
            name = f"{event.scope}: {event.variant}" if event.scope else event.variant
            self._running = self.heartbeat(
                lambda: self.print(f"… still running {name} #{event.index}, {elapsed(self.clock() - started)}"),
                self.interval)
            return
        result = "void, not counted" if event.result == "void" else event.result
        reused = " — reused from an earlier run" if event.reused else ""
        self.print(f"{describe(event)}: {result}, ${event.cost_usd:.2f} (episode spent ${event.spent_usd:.2f})"
                   f"{reused}")

    def print(self, line: str) -> None:
        print(line, file=self.stream, flush=True)
