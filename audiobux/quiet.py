"""Quiet hours: the card rests inside the window, work goes on when it ends.

`wait()` is the checkpoint: the pipeline calls it before every page, chapter and
round of pieces, and `check()` after every piece. Outside the window `wait()`
returns at once; inside it calls `on_pause` (the worker takes the models off the
card there), sleeps until the window ends and calls `on_resume`. `on_check` runs at
every checkpoint and every wake-up inside a pause: the worker stops a deleted book
there by raising.
"""

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

SPEC = re.compile(r"(\d\d):(\d\d)-(\d\d):(\d\d)")


class QuietHours:
    def __init__(self, spec: str) -> None:
        self.spec = spec.strip()
        self.start: int | None = None
        self.end: int | None = None
        if self.spec:
            m = SPEC.fullmatch(self.spec)
            h1, m1, h2, m2 = (int(g) for g in m.groups()) if m else (99, 0, 0, 0)
            if max(h1, h2) > 23 or max(m1, m2) > 59 or (h1, m1) == (h2, m2):
                raise ValueError("quiet hours are HH:MM-HH:MM, two different times of the day")
            self.start, self.end = h1 * 60 + m1, h2 * 60 + m2
        # the server's local time: the time zone is set on the machine, not here
        self.clock: Callable[[], datetime] = datetime.now
        self._wake = threading.Event()
        self.sleep: Callable[[float], None] = self._nap
        self.on_check: Callable[[], None] | None = None
        self.on_pause: Callable[[], None] | None = None
        self.on_resume: Callable[[], None] | None = None

    def seconds_left(self, now: datetime | None = None) -> float:
        """Seconds until the window ends, 0 if we are outside it."""
        if self.start is None or self.end is None:
            return 0.0
        now = now or self.clock()
        minute = now.hour * 60 + now.minute
        # a window across midnight is all but the day between its end and its start
        inside = self.start <= minute < self.end if self.start < self.end else not self.end <= minute < self.start
        if not inside:
            return 0.0
        end = now.replace(hour=self.end // 60, minute=self.end % 60, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return (end - now).total_seconds()

    def check(self) -> float:
        """The checkpoint without the pause: seconds of quiet hours left, 0 outside them."""
        if self.on_check:
            self.on_check()
        return self.seconds_left()

    def wait(self) -> None:
        left = self.check()
        if not left:
            return
        log.info("quiet hours, pausing for %d min", left // 60 + 1)
        if self.on_pause:
            self.on_pause()
        while left:
            self.sleep(min(left, 300))
            left = self.check()
        log.info("quiet hours are over, going on")
        if self.on_resume:
            self.on_resume()

    def interrupt(self) -> None:
        """Wakes a sleep early, so the worker sees a new or a deleted book at once."""
        self._wake.set()

    def _nap(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()
