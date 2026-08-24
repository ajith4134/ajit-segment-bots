"""price-level-sampler: every symbol's latest price, published on a cadence.

The feed was never what stopped this system covering more symbols. 193 prints a
second is nothing. What stopped it was that each print was handed to 66 parts --
12,707 deliveries a second, measured on 2026-08-23 -- and that total scales with
trading volume, which is exactly what grows when the universe grows.

Thirty-seven of those parts read nothing from a trade but the symbol, the price
and the moment it printed. This part computes that once, for every symbol, and
publishes it as one frame per venue per tick. What the readers receive stops
depending on how much the market trades, and stops depending on how many symbols
there are: 37 parts at four frames a second is 148 deliveries a second whether the
universe is thirty symbols or thirteen hundred.

**A frame is not a claim that every symbol in it just traded.** Each level carries
the moment it printed, and a symbol quiet for an hour says so. That distinction is
the whole of the last two days' work: a price and the moment it printed are one
fact, and a frame that stamped its own publication time onto every level would be
the same defect one layer up -- a message that is genuinely fresh carrying numbers
that are not.

Frames are per venue, and split when they would be too large, because the bus
refuses a datagram over 131,072 bytes: at 2,590 symbol-venue pairs one frame is
about 181 KB, and per venue about 91 KB. A split is counted, so that a universe
which has outgrown one frame is visible rather than inferred.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "price-level-sampler"

PART_DECLARATION = PartDeclaration(
    part_id="price-level-sampler",
    consumes=("market-data",),
    produces=("symbol-price-frame", "part-health"),
    resource_class="bandwidth-bound",
    # The cadence is what every reader's staleness bound is measured against, so a
    # frame published late carries levels older than its readers were told to
    # expect. Slowing this part changes what the parts downstream believe.
    rate_risk="changes-the-answer",
    # One frame not sent is one frame not sent. Every level in the next frame is
    # the current one: nothing accumulates and nothing is lost, because a level is
    # what is true now rather than an event that happened once.
    skipped_tick_effect="delays",
)


@dataclass(frozen=True)
class SymbolPriceLevel:
    """What one symbol last traded at, and when it did."""

    symbol: str
    price: float
    observed_at_ns: int

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


@dataclass(frozen=True)
class SymbolPriceFrame:
    """Every symbol's latest price on one venue, at one moment.

    `published_at_ns` is when the frame was sent and nothing else. It is never the
    age of any level in it, and a reader that used it as one would be reading the
    defect this part exists to prevent.
    """

    venue_id: str
    levels: tuple[SymbolPriceLevel, ...]
    published_at_ns: int
    part_number: int
    of_parts: int

    @property
    def was_split(self) -> bool:
        return self.of_parts > 1


@dataclass
class SamplerStanding:
    trades_observed: int = 0
    frames_published: int = 0
    frames_split: int = 0
    symbols_tracked: int = 0
    venues_tracked: int = 0
    largest_frame_symbols: int = 0


class PriceLevelSampler:
    """Holds the latest price per venue and symbol; publishes them together."""

    def __init__(
        self,
        cadence_seconds: float,
        maximum_symbols_per_frame: int,
        now_ns=time.time_ns,
    ) -> None:
        if not cadence_seconds > 0:
            raise ValueError(
                "the cadence is how often a frame is published, in seconds, and must be "
                f"positive; got {cadence_seconds!r}"
            )
        if maximum_symbols_per_frame < 1:
            raise ValueError(
                "a frame carries at least one symbol; a bound below that publishes nothing "
                f"while looking like a working sampler. Got {maximum_symbols_per_frame!r}"
            )
        self._cadence_ns = int(cadence_seconds * 1e9)
        self._maximum_symbols = maximum_symbols_per_frame
        self._now_ns = now_ns
        self._levels: dict[str, dict[str, SymbolPriceLevel]] = {}
        self._last_published_at_ns: int | None = None
        self.standing = SamplerStanding()

    def observe_trade(self, trade) -> None:
        """One print. The level it replaces keeps the venue's own time for it."""
        price = getattr(trade, "price", None)
        if price is None or price <= 0:
            return
        self.standing.trades_observed += 1
        venue = self._levels.setdefault(trade.venue_id, {})
        venue[trade.symbol] = SymbolPriceLevel(
            symbol=trade.symbol,
            price=float(price),
            observed_at_ns=int(trade.venue_time_ns),
        )
        self.standing.venues_tracked = len(self._levels)
        self.standing.symbols_tracked = sum(len(symbols) for symbols in self._levels.values())

    def frames_due(self, now_ns: int | None = None) -> tuple[SymbolPriceFrame, ...]:
        """The frames to publish at this moment, or none if the cadence has not elapsed.

        A venue that has printed nothing publishes no frame: an empty frame is not
        a fact about the market, it is a fact about the feed, and `feed-gap-detector`
        is the part that owns that.
        """
        at = self._now_ns() if now_ns is None else now_ns
        if self._last_published_at_ns is not None and at - self._last_published_at_ns < self._cadence_ns:
            return ()

        frames: list[SymbolPriceFrame] = []
        for venue_id in sorted(self._levels):
            levels = tuple(
                level for _, level in sorted(self._levels[venue_id].items())
            )
            if not levels:
                continue
            batches = [
                levels[start : start + self._maximum_symbols]
                for start in range(0, len(levels), self._maximum_symbols)
            ]
            if len(batches) > 1:
                self.standing.frames_split += 1
            for number, batch in enumerate(batches, start=1):
                frames.append(
                    SymbolPriceFrame(
                        venue_id=venue_id,
                        levels=batch,
                        published_at_ns=at,
                        part_number=number,
                        of_parts=len(batches),
                    )
                )
                self.standing.largest_frame_symbols = max(
                    self.standing.largest_frame_symbols, len(batch)
                )
        if not frames:
            return ()
        self._last_published_at_ns = at
        self.standing.frames_published += len(frames)
        return tuple(frames)


def describe_sampling(sampler: PriceLevelSampler) -> dict:
    return {
        "part_id": PART_ID,
        "trades_observed": sampler.standing.trades_observed,
        "frames_published": sampler.standing.frames_published,
        "frames_split": sampler.standing.frames_split,
        "symbols_tracked": sampler.standing.symbols_tracked,
        "venues_tracked": sampler.standing.venues_tracked,
        "largest_frame_symbols": sampler.standing.largest_frame_symbols,
    }


def run_price_level_sampler(
    sampler: PriceLevelSampler, control_socket, read_trades, publish_frames,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade in read_trades():
            sampler.observe_trade(trade)
        frames = sampler.frames_due()
        if frames:
            publish_frames(frames)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sampling(sampler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The cadence is decided rather than chosen: a level has to be fresher than the
    tightest age any symbol will believe, and that floor is
    `reference_price_minimum_age_seconds`. Publishing four times inside it leaves
    a factor of four, so a frame delayed by a tick still carries levels every
    reader will accept.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    trades = Batch(read=context.bus.reader("market-data"))
    publish_frames = context.bus.publisher_for("symbol-price-frame")

    def read_trades():
        return tuple(
            trade for trade in trades.payloads() if isinstance(trade, NormalisedTrade)
        )

    return run_price_level_sampler(
        sampler=PriceLevelSampler(
            cadence_seconds=context.number("price_frame_cadence_seconds"),
            maximum_symbols_per_frame=int(context.number("price_frame_maximum_symbols")),
        ),
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_frames=publish_frames,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
