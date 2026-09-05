"""A whole table, said evenly, forever, at a rate a consumer can drain.

A level is true until it changes, and `runtime/level_publishing.py` is how a
part avoids restating one nobody changed. This module is the opposite case and
the two are easily confused: a table that **has not** changed still has to be
said again, because the audience changes. The governor restarts a part far more
often than the instrument master is re-fetched, and a part that started a second
after the last fetch would otherwise wait the whole refresh interval holding
nothing.

Both halves of the rate matter, and both were measured on 2026-09-04 rather than
chosen:

- **Not once per refresh.** `broker-instrument-catalogue-reader` published its
  102,940 listings only when it re-fetched them, once an hour;
  `expiry-day-zero-to-hero-detector` had never held a single one.
- **Not all at once either.** A listing pickles to about 400 bytes against a
  default 212,992-byte socket buffer, which holds 532 of them, so one burst of
  102,940 overflows it 193 times over. `broker-market-feed-reader` was measured
  holding 1,067 listings of 102,940, with none of the three index underlyings
  the segment trades among them.

So the rate is the table divided by the cycle -- 57 a second for 102,940 rows at
1,800 s, a tenth of what one buffer holds -- and it is computed from **elapsed
time**, never as a fixed slice per tick: a part is woken by its clock and the
interval between ticks is not guaranteed, so a per-tick slice would speed up and
slow down with the machine's load. What is owed is capped at one cycle, so a long
pause does not become the burst this exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence


class RestatementConveyor:
    """Holds a table and hands back the next slice of it that is owed."""

    def __init__(self, cycle_seconds: float) -> None:
        if cycle_seconds <= 0:
            raise ValueError(
                "a cycle is how long one full restatement of the table takes; "
                f"got {cycle_seconds!r}"
            )
        self._cycle_seconds = cycle_seconds
        self._rows: Sequence = ()
        self._position = 0
        self._restated = 0
        self._cycles = 0
        self._last_at: float | None = None
        self._rate = 0.0

    def hold(self, rows: Sequence) -> None:
        """Replace the table, keeping the cycle where it is.

        **The position survives the swap, and that is the whole point.** A table
        that is rebuilt as its rows arrive is handed over often -- the
        subscribed listings change roughly once a second, since the master
        conveyor delivers a subscribed row about that often -- and a conveyor
        that restarted on every hand-over would republish the first slice at
        exactly the right rate, forever, and never reach the rest. Every counter
        would climb while most of the table was never said.

        Nothing is lost by keeping it: a row dropped from the new table stops
        being restated immediately, and a new one is reached within one cycle
        either way, because the cycle wraps.

        The position is taken modulo the new length, so a table that shrank does
        not leave the conveyor pointing past its end.
        """
        self._rows = rows
        self._position = self._position % len(rows) if rows else 0

    @property
    def rows(self) -> Sequence:
        return self._rows

    def due_slice(self, now: float) -> tuple:
        """The rows owed since this was last asked, in table order.

        Empty on the first call -- there is no elapsed time to owe a slice
        against yet -- and empty while the table is empty.
        """
        rows = self._rows
        if not rows:
            return ()
        self._rate = len(rows) / self._cycle_seconds
        last = self._last_at
        if last is None:
            self._last_at = now
            return ()
        owed = int(self._rate * (now - last))
        if owed <= 0:
            return ()
        owed = min(owed, len(rows))
        # Advanced by exactly what was said, not to `now`. Moving it to `now`
        # throws away the fraction of a row that was owed and not sent, every
        # time, so the cycle quietly runs longer than the setting says it does --
        # up to one row per slice, which at a one-second tick is most of the
        # rate. The remainder is kept instead, and the cycle honours its period.
        self._last_at = last + owed / self._rate

        position = self._position
        end = position + owed
        if end <= len(rows):
            slice_ = tuple(rows[position:end])
        else:
            slice_ = tuple(rows[position:]) + tuple(rows[: end - len(rows)])
            self._cycles += 1
        self._position = end % len(rows)
        self._restated += len(slice_)
        return slice_

    def standing(self) -> dict:
        """What the conveyor has said, and where in the table it is.

        `restated` climbing is the evidence a consumer that started after the
        last fetch will be told anything at all; `position` says where the next
        slice comes from, so a conveyor that has stopped turning is visible
        rather than merely quiet.
        """
        return {
            "restated": self._restated,
            "position": self._position,
            "cycles": self._cycles,
            "rate": self._rate,
        }


__all__ = ["RestatementConveyor"]
