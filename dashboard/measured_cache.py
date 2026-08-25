#!/usr/bin/env python3
"""A measurement held between requests, so a slow probe cannot be asked twice at once.

The failure this exists to close was live and public. `/api/board` measures the
filesystem for all 327 parts and takes about eleven seconds; the frontend polled
it every five. So a second request started while the first was still working, and
a third while those two were, each spawning a thread doing eleven seconds of
filesystem work. The server saturated, Cloudflare gave up at a hundred seconds,
and the operator's board answered `HTTP 524` -- while every part underneath it was
perfectly healthy.

Two rules, and they are the whole module:

**Never measure the same thing twice at once.** A second caller arriving during a
refresh waits for that refresh and takes its answer, rather than starting another.

**Never hide the age.** The payload always carries `measured_at` and
`measured_age_seconds`, because a cached answer served as if it were fresh is the
Rule 8 failure one layer along -- the board would look live while showing a
picture minutes old, and nothing would say so.

A cache is the right answer here only because of what is being cached: the board
payload describes the *code*, which changes on deploy and not between two polls a
second apart. Live behaviour is never cached -- `/api/activity` reads one small
file and is cheap on purpose.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class MeasuredCache:
    """One expensive measurement, refreshed no more often than `fresh_for_seconds`.

    `refresh` is called with no arguments and returns the payload. It runs under a
    lock, so concurrent callers cost one measurement rather than one each.
    """

    refresh: object
    fresh_for_seconds: float
    _payload: dict | None = None
    _measured_at_ns: int | None = None
    _lock: threading.Lock | None = None
    _measurements: int = 0
    _served_from_cache: int = 0

    def __post_init__(self) -> None:
        if self.fresh_for_seconds < 0:
            raise ValueError("a negative freshness window would refuse to ever serve")
        self._lock = threading.Lock()

    def age_seconds(self, now_ns: int | None = None) -> float | None:
        if self._measured_at_ns is None:
            return None
        return ((now_ns or time.time_ns()) - self._measured_at_ns) / 1e9

    def is_fresh(self, now_ns: int | None = None) -> bool:
        age = self.age_seconds(now_ns)
        return age is not None and age < self.fresh_for_seconds

    def read(self) -> dict:
        """The measurement, taken now if it is stale and someone else is not taking it.

        The fast path takes no lock at all: once a payload exists and is inside its
        window, this is a dict lookup, which is what makes an eleven-second probe
        answerable in microseconds.
        """
        if self.is_fresh():
            self._served_from_cache += 1
            return self._stamped(self._payload)

        with self._lock:
            # Re-checked inside the lock. Whoever held it may have just refreshed,
            # and measuring again immediately is the pile-up this class prevents.
            if self.is_fresh():
                self._served_from_cache += 1
                return self._stamped(self._payload)
            payload = self.refresh()
            self._payload = payload
            self._measured_at_ns = time.time_ns()
            self._measurements += 1
            return self._stamped(payload)

    def _stamped(self, payload: dict) -> dict:
        """The payload with its own age attached, never without it."""
        stamped = dict(payload or {})
        stamped["measured_age_seconds"] = self.age_seconds()
        stamped["measured_at_ns"] = self._measured_at_ns
        stamped["cache"] = {
            "fresh_for_seconds": self.fresh_for_seconds,
            "measurements_taken": self._measurements,
            "served_from_cache": self._served_from_cache,
        }
        return stamped
