"""quote-level-sampler: every symbol's latest bid and ask, published on a cadence.

The same argument that produced `price-level-sampler`, on a louder feed. Measured
2026-08-24: Bybit's quote stream carries 1,190 updates a second against roughly
224 of trades at 100 symbols per venue, and Binance's carries about 107. Handing
each one to every reader would re-create exactly the fan-out that sampling the
trade feed removed -- and would do it on the stream that arrives five times
faster.

So this part holds the latest quote per venue and symbol, and publishes them
together on a fixed cadence. What a reader receives stops depending on how much
the market quotes.

**A frame is not a claim that every symbol in it just quoted.** Each level carries
the moment the venue stamped it, and a symbol nobody has quoted for a minute says
so. The reader upstream has already done the harder half of that: a quote merged
out of one-sided deltas is dated by its stalest side, never its freshest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.frame_splitting import batches_that_fit, frame_size_measured_by
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "quote-level-sampler"
# The wire this part's frames ride, named once so the splitter measures a frame
# against the same type the publisher will send it as.
FRAME_TYPE = "symbol-quote-frame"

PART_DECLARATION = PartDeclaration(
    part_id="quote-level-sampler",
    consumes=("market-quote",),
    produces=("symbol-quote-frame", "part-health"),
    resource_class="compute-bound",
    # Every reader's staleness bound is measured against this cadence, so a frame
    # published late carries quotes older than its readers were told to expect.
    rate_risk="changes-the-answer",
    # One frame not sent is one frame not sent: every level in the next frame is
    # the current one, because a quote is what is true now rather than an event.
    skipped_tick_effect="delays",
)


@dataclass(frozen=True)
class SymbolQuoteLevel:
    """What one symbol's best bid and ask are, and when the venue said so."""

    symbol: str
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    observed_at_ns: int

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


@dataclass(frozen=True)
class SymbolQuoteFrame:
    """Every symbol's latest quote on one venue, at one moment.

    `published_at_ns` is when the frame was sent and nothing else. It is never the
    age of any level in it, and a reader using it as one would be committing the
    defect this whole design exists to prevent.
    """

    venue_id: str
    levels: tuple[SymbolQuoteLevel, ...]
    published_at_ns: int
    part_number: int
    of_parts: int

    @property
    def was_split(self) -> bool:
        return self.of_parts > 1


@dataclass
class QuoteSamplerStanding:
    quotes_observed: int = 0
    frames_published: int = 0
    frames_split: int = 0
    symbols_tracked: int = 0
    venues_tracked: int = 0
    largest_frame_symbols: int = 0
    crossed_quotes_refused: int = 0


class QuoteLevelSampler:
    """Holds the latest quote per venue and symbol; publishes them together."""

    def __init__(
        self,
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
                "a frame carries at least one symbol; a bound below that publishes nothing "
                f"while looking like a working sampler. Got {maximum_symbols_per_frame!r}"
            )
        if maximum_frame_bytes < 1:
            raise ValueError(
                "the byte bound is what the bus will actually carry, and must be positive; "
                f"got {maximum_frame_bytes!r}"
            )
        self._cadence_ns = int(cadence_seconds * 1e9)
        self._maximum_symbols = maximum_symbols_per_frame
        self._maximum_frame_bytes = maximum_frame_bytes
        self._now_ns = now_ns
        self._levels: dict[str, dict[str, SymbolQuoteLevel]] = {}
        self._last_published_at_ns: int | None = None
        self.standing = QuoteSamplerStanding()

    def observe_quote(self, quote) -> None:
        """One quote. The level it replaces keeps the venue's own time for it."""
        bid = getattr(quote, "bid_price", None)
        ask = getattr(quote, "ask_price", None)
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return
        if bid > ask:
            # A crossed quote is not a market, it is a moment between two venue
            # messages caught mid-update. Sizing against its mid would price a
            # position at a level nobody offered. Counted, because a rate that is
            # not near zero says the merge upstream is pairing sides that were
            # never simultaneous.
            self.standing.crossed_quotes_refused += 1
            return
        self.standing.quotes_observed += 1
        venue = self._levels.setdefault(quote.venue_id, {})
        venue[quote.symbol] = SymbolQuoteLevel(
            symbol=quote.symbol,
            bid_price=float(bid),
            bid_quantity=float(quote.bid_quantity),
            ask_price=float(ask),
            ask_quantity=float(quote.ask_quantity),
            observed_at_ns=int(quote.venue_time_ns),
        )
        self.standing.venues_tracked = len(self._levels)
        self.standing.symbols_tracked = sum(len(symbols) for symbols in self._levels.values())

    def _frame_size_of(self, venue_id: str):
        """What a batch of this venue's levels would weigh as a frame on the bus."""
        return frame_size_measured_by(
            data_type=FRAME_TYPE,
            producer_part_id=PART_ID,
            build_payload=lambda batch, part_number, of_parts: SymbolQuoteFrame(
                venue_id=venue_id,
                levels=tuple(batch),
                published_at_ns=part_number,
                part_number=part_number,
                of_parts=of_parts,
            ),
        )

    def frames_due(self, now_ns: int | None = None) -> tuple[SymbolQuoteFrame, ...]:
        """The frames to publish at this moment, or none if the cadence has not elapsed.

        A venue that has quoted nothing publishes no frame: an empty frame is a
        fact about the feed rather than about the market, and `feed-gap-detector`
        is the part that owns that.
        """
        at = self._now_ns() if now_ns is None else now_ns
        if (
            self._last_published_at_ns is not None
            and at - self._last_published_at_ns < self._cadence_ns
        ):
            return ()

        frames: list[SymbolQuoteFrame] = []
        for venue_id in sorted(self._levels):
            levels = tuple(level for _, level in sorted(self._levels[venue_id].items()))
            if not levels:
                continue
            batches = batches_that_fit(
                levels,
                most_items_per_batch=self._maximum_symbols,
                maximum_bytes=self._maximum_frame_bytes,
                size_of=self._frame_size_of(venue_id),
            )
            if len(batches) > 1:
                self.standing.frames_split += 1
            for number, batch in enumerate(batches, start=1):
                frames.append(
                    SymbolQuoteFrame(
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


def describe_quote_sampling(sampler: QuoteLevelSampler) -> dict:
    return {
        "part_id": PART_ID,
        "quotes_observed": sampler.standing.quotes_observed,
        "frames_published": sampler.standing.frames_published,
        "frames_split": sampler.standing.frames_split,
        "symbols_tracked": sampler.standing.symbols_tracked,
        "venues_tracked": sampler.standing.venues_tracked,
        "largest_frame_symbols": sampler.standing.largest_frame_symbols,
        "crossed_quotes_refused": sampler.standing.crossed_quotes_refused,
    }


def run_quote_level_sampler(
    sampler: QuoteLevelSampler,
    control_socket,
    read_quotes,
    publish_frames,
    health_interval_seconds: float,
    emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for quote in read_quotes():
            sampler.observe_quote(quote)
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
        read_standing=lambda: describe_quote_sampling(sampler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The cadence is the trade sampler's, deliberately: a reader holding both frames
    compares a price against a quote, and two feeds sampled at different rates
    would make the newer one look right whenever they disagreed.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedQuote

    quotes = Batch(read=context.bus.reader("market-quote"))
    publish_frames = context.bus.publisher_for("symbol-quote-frame")

    def read_quotes():
        return tuple(
            quote for quote in quotes.payloads() if isinstance(quote, NormalisedQuote)
        )

    return run_quote_level_sampler(
        sampler=QuoteLevelSampler(
            # The bus's own ceiling, not a second number that could drift from it:
            # the frame is split against the size the thing carrying it enforces.
            maximum_frame_bytes=int(context.number("maximum_message_bytes")),
            cadence_seconds=context.number("price_frame_cadence_seconds"),
            maximum_symbols_per_frame=int(context.number("price_frame_maximum_symbols")),
        ),
        control_socket=context.control_socket,
        read_quotes=read_quotes,
        publish_frames=publish_frames,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "QuoteLevelSampler",
    "QuoteSamplerStanding",
    "SymbolQuoteFrame",
    "SymbolQuoteLevel",
    "describe_quote_sampling",
    "run_quote_level_sampler",
    "start_part",
]
