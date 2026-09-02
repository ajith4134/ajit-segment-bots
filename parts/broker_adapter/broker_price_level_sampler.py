"""broker-price-level-sampler: every tracked instrument's latest price,
published on a cadence -- the Indian-markets analogue of the crypto build's
price-level-sampler (docs/proposals/broker-price-quote-samplers.md).

Same reasoning as the crypto part: many downstream consumers read only the
instrument, the price and the moment it printed out of every LTP update.
Computing that once and republishing on a cadence stops delivery volume
scaling with tick rate or universe size.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.frame_splitting import batches_that_fit, frame_size_measured_by
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-price-level-sampler"
# The wire this part's frames ride, named once so the splitter measures a frame
# against the same type the publisher will send it as.
FRAME_TYPE = "broker-price-frame"

PART_DECLARATION = PartDeclaration(
    part_id="broker-price-level-sampler",
    consumes=("broker-market-data",),
    produces=("broker-price-frame", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


@dataclass(frozen=True)
class BrokerPriceLevel:
    """What one instrument last traded at, and when it did."""

    instrument_key: str
    price: float
    observed_at_ns: int

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


@dataclass(frozen=True)
class BrokerPriceFrame:
    """Every instrument's latest price on one broker, at one moment.

    `published_at_ns` is when the frame was sent and nothing else -- never
    the age of any level in it.
    """

    broker_id: str
    levels: tuple[BrokerPriceLevel, ...]
    published_at_ns: int
    part_number: int
    of_parts: int

    @property
    def was_split(self) -> bool:
        return self.of_parts > 1


@dataclass
class SamplerStanding:
    updates_observed: int = 0
    frames_published: int = 0
    frames_split: int = 0
    instruments_tracked: int = 0


class BrokerPriceLevelSampler:
    """Holds the latest price per instrument for one broker; publishes them together."""

    def __init__(
        self,
        broker_id: str,
        cadence_seconds: float,
        maximum_symbols_per_frame: int,
        maximum_frame_bytes: int,
        now_ns=time.time_ns,
    ) -> None:
        if not cadence_seconds > 0:
            raise ValueError(
                "the cadence is how often a frame is published, in seconds, and must be "
                f"positive; got {cadence_seconds!r}"
            )
        if maximum_symbols_per_frame < 1:
            raise ValueError(
                "a frame carries at least one instrument; a bound below that publishes "
                f"nothing while looking like a working sampler. Got {maximum_symbols_per_frame!r}"
            )
        if maximum_frame_bytes < 1:
            raise ValueError(
                "the byte bound is what the bus will actually carry, and must be positive; "
                f"got {maximum_frame_bytes!r}"
            )
        self._broker_id = broker_id
        self._cadence_ns = int(cadence_seconds * 1e9)
        self._maximum_symbols = maximum_symbols_per_frame
        self._maximum_frame_bytes = maximum_frame_bytes
        self._now_ns = now_ns
        self._levels: dict[str, BrokerPriceLevel] = {}
        self._last_published_at_ns: int | None = None
        self.standing = SamplerStanding()

    def observe_ltp(self, update) -> None:
        """One LTP update. The level it replaces keeps the broker's own time for it."""
        price = getattr(update, "last_traded_price", None)
        if price is None or price <= 0:
            return
        self.standing.updates_observed += 1
        self._levels[update.instrument_key] = BrokerPriceLevel(
            instrument_key=update.instrument_key,
            price=float(price),
            observed_at_ns=int(update.last_traded_time_ms) * 1_000_000,
        )
        self.standing.instruments_tracked = len(self._levels)

    def _frame_size_of(self):
        """What a batch of these levels would weigh as a frame on the bus."""
        return frame_size_measured_by(
            data_type=FRAME_TYPE,
            producer_part_id=PART_ID,
            build_payload=lambda batch, part_number, of_parts: BrokerPriceFrame(
                broker_id=self._broker_id,
                levels=tuple(batch),
                published_at_ns=part_number,
                part_number=part_number,
                of_parts=of_parts,
            ),
        )

    def frames_due(self, now_ns: int | None = None) -> tuple[BrokerPriceFrame, ...]:
        """The frames to publish at this moment, or none if the cadence has not elapsed.

        No update seen at all publishes no frame -- an empty frame is a fact
        about the feed, not about the market.
        """
        at = self._now_ns() if now_ns is None else now_ns
        if self._last_published_at_ns is not None and at - self._last_published_at_ns < self._cadence_ns:
            return ()
        if not self._levels:
            return ()

        levels = tuple(level for _, level in sorted(self._levels.items()))
        batches = batches_that_fit(
            levels,
            most_items_per_batch=self._maximum_symbols,
            maximum_bytes=self._maximum_frame_bytes,
            size_of=self._frame_size_of(),
        )
        if len(batches) > 1:
            self.standing.frames_split += 1
        frames = tuple(
            BrokerPriceFrame(
                broker_id=self._broker_id, levels=batch,
                published_at_ns=at, part_number=number, of_parts=len(batches),
            )
            for number, batch in enumerate(batches, start=1)
        )
        self._last_published_at_ns = at
        self.standing.frames_published += len(frames)
        return frames


def describe_sampling(sampler: BrokerPriceLevelSampler) -> dict:
    return {
        "part_id": PART_ID,
        "updates_observed": sampler.standing.updates_observed,
        "frames_published": sampler.standing.frames_published,
        "frames_split": sampler.standing.frames_split,
        "instruments_tracked": sampler.standing.instruments_tracked,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.brokers.broker_adapter import LtpUpdate
    from runtime.brokers.upstox import UPSTOX_BROKER_ID
    from runtime.input_assembly import Batch

    updates = Batch(read=context.bus.reader("broker-market-data"))
    publish_frames = context.bus.publisher_for("broker-price-frame")
    sampler = BrokerPriceLevelSampler(
        broker_id=UPSTOX_BROKER_ID,
        # The bus's own ceiling, not a second number that could drift from it:
        # the frame is split against the size the thing carrying it enforces.
        maximum_frame_bytes=int(context.number("maximum_message_bytes")),
        cadence_seconds=context.number("broker_price_frame_cadence_seconds"),
        maximum_symbols_per_frame=int(context.number("broker_price_frame_maximum_symbols")),
    )

    def tick() -> None:
        for update in updates.payloads():
            if isinstance(update, LtpUpdate):
                sampler.observe_ltp(update)
        frames = sampler.frames_due()
        if frames:
            publish_frames(frames)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_sampling(sampler),
    )


__all__ = [
    "BrokerPriceFrame",
    "BrokerPriceLevel",
    "BrokerPriceLevelSampler",
    "PART_DECLARATION",
    "PART_ID",
    "SamplerStanding",
    "describe_sampling",
    "start_part",
]
