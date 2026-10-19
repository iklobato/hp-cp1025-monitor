"""One polling round of the printer, shared by the terminal monitors and the web server."""

import time
from collections import deque
from dataclasses import dataclass

import history
from printer_model import PrinterReader, Report

# A full reading measured 200-500 ms; waiting at least 4x that keeps the USB link idle
# most of the time, and never polling faster than once a second keeps it calm when fast.
MIN_INTERVAL_SECONDS = 1.0
DUTY_FACTOR = 4


def interval_after(read_seconds):
    return max(MIN_INTERVAL_SECONDS, read_seconds * DUTY_FACTOR)


@dataclass(frozen=True)
class Reading:
    """Everything one round produced; the front ends only render this."""

    report: Report
    entries: list
    prediction: history.Forecast
    changes: tuple
    number: int
    read_ms: float
    next_in: float


class Poller:
    """Reads, records history, diffs against the previous reading and paces the next one."""

    def __init__(
        self, reader=None, record=history.record, feed_size=12, clock=time.monotonic
    ):
        self._reader = reader or PrinterReader()
        self._record = record
        self._clock = clock
        self._changes = deque(maxlen=feed_size)
        self._previous = None
        self._count = 0

    def tick(self):
        """One reading. USB failures propagate; the previous reading stays the diff base."""
        started = self._clock()
        report = self._reader.read()
        read_seconds = self._clock() - started
        entries = self._record(report)
        if self._previous is not None:
            self._changes.extend(
                (report.read_at, change)
                for change in report.changes_since(self._previous)
            )
        self._previous = report
        self._count += 1
        return Reading(
            report=report,
            entries=entries,
            prediction=history.forecast(entries),
            changes=tuple(self._changes),
            number=self._count,
            read_ms=read_seconds * 1000,
            next_in=interval_after(read_seconds),
        )
