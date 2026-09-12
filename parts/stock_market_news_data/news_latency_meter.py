"""news-latency-meter: measure how late this system sees a news item.

Not telemetry. `news-impact-forecaster` reads `news-latency-reading` and
discounts a late item, because the move is already gone — an item seen forty
seconds late deserves less weight than one seen in two, and the block's own
design says so. This part is where that number comes from.

## "Late" only means anything against a source's own habit

Twenty seconds is late for an exchange filing feed and early for a daily news
digest, so this part measures each source separately and hands the consumer
both numbers: this item's latency, and what is ordinary for the source that
carried it. A single threshold shared across sources would report a broker's
news API as permanently late and a filing feed as permanently fine.

Measured on the real Upstox capture of 2026-09-12: the newest of 14 stories was
published 2026-09-11 14:27 UTC and first seen 21.5 hours later, and the
response carried a backlog spanning **153.3 hours**. That is the honest reading
of this source — it is a digest, not a wire — and it is exactly the kind of
fact a system trading on news must know rather than assume.

## Two latencies that must not be averaged together

A part that starts looking today sees a six-day-old story for the first time
today. Its latency is six days, and that measures **when this system was
switched on**, not how late the source is. Mixed into the same distribution it
makes every restart look like a news outage and would have
`news-impact-forecaster` discount every real story for hours after a restart.

So an item published before this meter started is counted, reported, and kept
out of the quantile: `was_already_published_when_the_meter_started` travels on
the reading, and `backlog_items` is its own counter in the standing. Rule 8 —
the two facts are visibly different rather than one number that quietly blends
them.

## Negative latency is a reading, not an error

A source whose clock runs ahead of this machine's produces an item published in
the future. `RawNewsItem.latency_ns` does not clamp it and neither does this
part: clamping hides exactly the skew `clock-skew-monitor` exists to find. It
is counted under `items_published_in_the_future` and excluded from the
quantile, because a negative number is not evidence about how fast a source
delivers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.learned_estimator import QuantileEstimator
from runtime.news_types import NewsLatencyReading
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "news-latency-meter"

PART_DECLARATION = PartDeclaration(
    part_id="news-latency-meter",
    consumes=("raw-news-item",),
    produces=("news-latency-reading", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NANOSECONDS_PER_SECOND = 1_000_000_000

# The quantile that stands for "what is ordinary for this source". The median,
# not the mean and not a tail: the reading's job is to say whether THIS item was
# unusually late for its source, and a tail quantile would call an ordinary item
# early. `news-impact-forecaster` is the part that decides what to do about a
# multiple of it.
TYPICAL_LATENCY_QUANTILE = 0.5


@dataclass
class MeterStanding:
    items_read: int = 0
    readings_published: int = 0
    # Published before this meter started looking: their latency says when this
    # system was switched on, not how late the source is.
    backlog_items: int = 0
    items_published_in_the_future: int = 0
    items_with_no_source: int = 0
    sources_measured: int = 0
    # The latency actually observed, so the standing itself answers "is this
    # system reacting to news or chasing it" without a consumer.
    slowest_latency_seconds: float = 0.0
    fastest_latency_seconds: float | None = None


class NewsLatencyMeter:
    """How late each source's news arrives, learned from its own arrivals."""

    def __init__(
        self,
        started_at_ns: int,
        window: int,
        minimum_observations: int,
        prior_latency_seconds: float,
    ) -> None:
        if window < 1:
            raise ValueError("the window must hold at least one observation")
        if minimum_observations < 1:
            raise ValueError(
                "minimum_observations must be at least one: it is how many arrivals a "
                "source must have before this part will state a typical latency for "
                "it, and zero would state one from nothing"
            )
        self._started_at_ns = started_at_ns
        self._window = window
        self._minimum_observations = minimum_observations
        self._prior_latency_seconds = prior_latency_seconds
        self._latency_by_source: dict[str, QuantileEstimator] = {}
        self.standing = MeterStanding()

    def observe(self, item) -> NewsLatencyReading | None:
        """One raw item. What its latency reading is."""
        self.standing.items_read += 1
        source_id = str(getattr(item, "source_id", "") or "")
        if not source_id:
            # A latency belongs to a source. An item that does not say which
            # one it came from cannot be attributed, and attributing it to a
            # catch-all would pollute every source's measured habit with
            # another's. Counted so a source that stops naming itself shows up.
            self.standing.items_with_no_source += 1
            return None

        published_at_ns = int(getattr(item, "published_at_ns", 0) or 0)
        observed_at_ns = int(getattr(item, "observed_at_ns", 0) or 0)
        if not published_at_ns or not observed_at_ns:
            return None
        latency_ns = observed_at_ns - published_at_ns

        was_backlog = published_at_ns < self._started_at_ns
        if was_backlog:
            self.standing.backlog_items += 1
        if latency_ns < 0:
            self.standing.items_published_in_the_future += 1

        estimator = self._latency_by_source.get(source_id)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._window,
                prior=self._prior_latency_seconds * NANOSECONDS_PER_SECOND,
            )
            self._latency_by_source[source_id] = estimator
            self.standing.sources_measured = len(self._latency_by_source)

        if not was_backlog and latency_ns >= 0:
            # Only a latency this system actually waited through teaches
            # anything about how fast the source is.
            estimator.observe(latency_ns)
            self._note_extremes(latency_ns)

        estimate = estimator.estimate(
            quantile=TYPICAL_LATENCY_QUANTILE,
            minimum_observations=self._minimum_observations,
        )
        self.standing.readings_published += 1
        return NewsLatencyReading(
            source_id=source_id,
            story_key=str(getattr(item, "url", "") or getattr(item, "title", "") or ""),
            published_at_ns=published_at_ns,
            observed_at_ns=observed_at_ns,
            latency_ns=latency_ns,
            # None rather than the prior when nothing is fitted yet: a
            # consumer discounting an item against a typical latency nobody
            # measured is discounting it against a guess (Rule 8).
            typical_latency_ns=int(estimate.value) if estimate.is_fitted else None,
            observations=estimate.observations,
            was_already_published_when_the_meter_started=was_backlog,
        )

    def _note_extremes(self, latency_ns: int) -> None:
        seconds = latency_ns / NANOSECONDS_PER_SECOND
        if seconds > self.standing.slowest_latency_seconds:
            self.standing.slowest_latency_seconds = seconds
        if (
            self.standing.fastest_latency_seconds is None
            or seconds < self.standing.fastest_latency_seconds
        ):
            self.standing.fastest_latency_seconds = seconds

    def typical_latency_seconds(self, source_id: str) -> float | None:
        """What is ordinary for one source, or None while nothing is fitted."""
        estimator = self._latency_by_source.get(source_id)
        if estimator is None:
            return None
        estimate = estimator.estimate(
            quantile=TYPICAL_LATENCY_QUANTILE,
            minimum_observations=self._minimum_observations,
        )
        if not estimate.is_fitted:
            return None
        return estimate.value / NANOSECONDS_PER_SECOND


def describe_metering(meter: NewsLatencyMeter) -> dict:
    standing = meter.standing
    return {
        "part_id": PART_ID,
        "items_read": standing.items_read,
        "readings_published": standing.readings_published,
        "backlog_items": standing.backlog_items,
        "items_published_in_the_future": standing.items_published_in_the_future,
        "items_with_no_source": standing.items_with_no_source,
        "sources_measured": standing.sources_measured,
        "slowest_latency_seconds": standing.slowest_latency_seconds,
        "fastest_latency_seconds": standing.fastest_latency_seconds,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    items = Batch(read=context.bus.reader("raw-news-item"))
    publish_readings = context.bus.publisher_for("news-latency-reading")
    meter = NewsLatencyMeter(
        # When this part started looking, which is the line between a story
        # that reached the system late and one that was already old when the
        # system first looked.
        started_at_ns=time.time_ns(),
        window=int(context.number("news_latency_window_observations")),
        minimum_observations=int(
            context.number("news_latency_minimum_observations_to_state_a_typical")
        ),
        prior_latency_seconds=context.number("news_latency_prior_seconds"),
    )

    def tick() -> None:
        readings = []
        for item in items.payloads():
            reading = meter.observe(item)
            if reading is not None:
                readings.append(reading)
        if readings:
            publish_readings(tuple(readings))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_metering(meter),
    )


__all__ = [
    "MeterStanding",
    "NewsLatencyMeter",
    "PART_DECLARATION",
    "PART_ID",
    "TYPICAL_LATENCY_QUANTILE",
    "describe_metering",
    "start_part",
]
