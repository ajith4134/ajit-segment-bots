"""A range measured over a handful of prints is an underestimate. By how much.

The cold-start stop is a multiple of the range a symbol traded through over the
horizon its detector claimed. That range is measured from the prints that
actually arrived inside the window, and a window holding five prints spans less
of the symbol's real movement than one holding fifty -- not because the symbol
moved less, but because nobody was looking in between.

`*_cold_start_minimum_prints` was 20 with this provenance: *"Measured on the tape
2026-08-22: BTCUSDT prints about 4 a second, so a 60s horizon carries roughly 240
-- the bar binds on quiet symbols, which is exactly where it should."* A crypto
perpetual prints 240 times a minute; the median NSE option prints five. So the
bar refused almost every plan: measured on the live spine 2026-09-08,
`bull-exit-plan-proposer` refused **84,838 of 86,943** requests as
`too-few-prints-in-the-window-to-measure-a-range`, and the bear peer 71,211 of
77,282.

**Lowering the bar alone is the wrong fix and would be dangerous.** The range is
what the stop distance is measured from, so an under-measured range is a stop
placed too tight -- a machine for being stopped out of trades that were going to
work. Correcting the estimator is the fix, and the correction is measured:
holding the window fixed at 300 s and drawing n of the prints inside it, over 300
symbols and 1,974 windows of this project's own tape for 2026-09-08, the share of
the true range a sample of n recovers at the median is

    prints    3      4      5      6      8     10     15     20     30
    p50    41.4%  52.9%  60.2%  65.7%  73.0%  76.9%  85.1%  89.2%  94.2%

(measurements/2026-09-08-a-range-from-a-small-sample/, which reproduces the table
the 2026-09-07 starvation measurement reported and whose script was never saved.)

So a range measured over five prints is divided by 0.602 to state what the
window's whole range probably was. The median is used rather than a lower
quantile deliberately: the median is the unbiased estimate, and over-correcting
is not the safe direction it looks like -- a wider stop is a larger risk
fraction, which makes the round trip cheaper in units of risk and so *lowers* the
conviction floor. Being generous with the correction would buy fewer stop-outs by
taking more trades, which is not a trade-off this module is entitled to make.

Below the smallest measured count nothing is corrected and the range is refused,
because a 2.4x correction from three prints is guesswork wearing a measurement's
clothes.
"""

from __future__ import annotations

from bisect import bisect_left


class RecoveryTableRefused(ValueError):
    """The table cannot be read as a correction."""


class RangeFromASmallSample:
    """Corrects a range for how few prints it was measured over."""

    def __init__(
        self,
        print_counts: tuple[int, ...],
        recovered_fractions: tuple[float, ...],
    ) -> None:
        if len(print_counts) != len(recovered_fractions):
            raise RecoveryTableRefused(
                f"{len(print_counts)} print count(s) against "
                f"{len(recovered_fractions)} recovered fraction(s). They are read position "
                f"by position, so a mismatch is a count with no measurement or a "
                f"measurement of nothing."
            )
        if not print_counts:
            raise RecoveryTableRefused(
                "an empty recovery table corrects nothing while looking like a correction"
            )
        if list(print_counts) != sorted(print_counts):
            raise RecoveryTableRefused("the print counts must ascend, so a lookup can bracket")
        if any(count < 2 for count in print_counts):
            raise RecoveryTableRefused(
                "a range over one print is that print, reported with the authority of a range"
            )
        if any(not 0.0 < fraction <= 1.0 for fraction in recovered_fractions):
            raise RecoveryTableRefused(
                "each recovered share is a fraction of the true range in (0, 1]; a zero "
                "would divide by nothing and above one is not a recovery"
            )
        self._counts = tuple(int(count) for count in print_counts)
        self._recovered = tuple(float(fraction) for fraction in recovered_fractions)

    @property
    def fewest_prints_correctable(self) -> int:
        """Below this nothing is known about the bias, so nothing is claimed."""
        return self._counts[0]

    def recovered_share_at(self, prints: int) -> float | None:
        """What share of the true range a sample of this size spans, at the median.

        Linear between the measured counts, because the measurement is a handful
        of points on a smooth curve and the alternative -- snapping to the
        nearest measured count -- would step the stop distance discontinuously as
        one more print arrives.
        """
        if prints < self._counts[0]:
            return None
        if prints >= self._counts[-1]:
            return 1.0
        at = bisect_left(self._counts, prints)
        if self._counts[at] == prints:
            return self._recovered[at]
        below, above = self._counts[at - 1], self._counts[at]
        span = above - below
        weight = (prints - below) / span
        return self._recovered[at - 1] + weight * (self._recovered[at] - self._recovered[at - 1])

    def corrected(self, measured_range_fraction: float, prints: int) -> float | None:
        """What the window's whole range probably was, or None if too few to say."""
        recovered = self.recovered_share_at(prints)
        if recovered is None:
            return None
        return measured_range_fraction / recovered


__all__ = ["RangeFromASmallSample", "RecoveryTableRefused"]
