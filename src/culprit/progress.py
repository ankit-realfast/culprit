"""What the CLI shows while replays run: one stderr line per finished replay."""

from __future__ import annotations

import sys
from typing import TextIO

from culprit.experiment import ReplayEvent


def describe(event: ReplayEvent) -> str:
    """`baseline #2 of 5` — prefixed with the scope (planted fault, episode) when there is one."""
    name = f"{event.variant} #{event.index} of {event.n}"
    return f"{event.scope}: {name}" if event.scope else name


class ProgressPrinter:
    """The progress callback the CLI hands to run_variant, diagnose, run_selftest and diagnose_all."""

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stderr

    def __call__(self, event: ReplayEvent) -> None:
        if event.kind != "finish":
            return
        result = "void, not counted" if event.result == "void" else event.result
        reused = " — reused from an earlier run" if event.reused else ""
        self.print(f"{describe(event)}: {result}, ${event.cost_usd:.2f} (episode spent ${event.spent_usd:.2f})"
                   f"{reused}")

    def print(self, line: str) -> None:
        print(line, file=self.stream, flush=True)
