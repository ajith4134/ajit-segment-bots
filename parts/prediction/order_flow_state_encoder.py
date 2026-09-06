"""order-flow-state-encoder: every second of the tape as one of fifteen states.

The first of the three parts from `docs/research/order-flow-entropy.md`
(arXiv:2512.15720). It turns trades into the symbol sequence the entropy meter
reads, and every choice in it comes from that specification rather than from
taste.

**Fifteen states: the cross product of price-change sign and volume quintile.**

    q = sgn(P_t - P_{t-1})   in {-1, 0, +1}
    v = ceil(5 * F_V(V_t))   in {1..5}
    S = (q, v)               15 states

**The quintile is relative to the trailing 120 seconds, not to a constant.** That
single detail is what makes the measure self-normalising: a fixed volume
threshold would call every second of a quiet symbol "low volume" and every second
of a busy one "high", and the entropy of that sequence would describe the symbol's
size rather than its order flow.

**Seconds with no trades are a state, not a gap.** A second in which nothing
traded is information about the flow -- it is the quietest possible second -- and
skipping it would splice two non-adjacent seconds into a transition that never
happened.

**A second is closed before it is encoded.** Encoding a second still receiving
trades would produce a state that changes after the transition it belongs to has
been counted.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "order-flow-state-encoder"

PART_DECLARATION = PartDeclaration(
    part_id="order-flow-state-encoder",
    consumes=("market-data", "order-book-snapshot"),
    produces=("order-flow-state", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# From the specification: three signs times five quintiles.
PRICE_SIGNS = (-1, 0, 1)
VOLUME_QUINTILES = (1, 2, 3, 4, 5)
STATE_COUNT = len(PRICE_SIGNS) * len(VOLUME_QUINTILES)

NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class OrderFlowState:
    """One second, labelled. `index` is what the transition matrix is built on."""

    venue_id: str
    symbol: str
    second_ns: int
    price_sign: int
    volume_quintile: int
    close: float
    volume: float
    trades: int

    @property
    def index(self) -> int:
        return PRICE_SIGNS.index(self.price_sign) * len(VOLUME_QUINTILES) + (
            self.volume_quintile - 1
        )

    def __str__(self) -> str:
        return f"({self.price_sign:+d},{self.volume_quintile})"


@dataclass
class SecondUnderConstruction:
    second_ns: int
    close: float = 0.0
    volume: float = 0.0
    trades: int = 0


@dataclass
class EncoderStanding:
    trades_seen: int = 0
    seconds_encoded: int = 0
    empty_seconds_encoded: int = 0
    symbols_tracked: int = 0
    by_state: dict = field(default_factory=dict)
    quintile_windows_short: int = 0


class OrderFlowStateEncoder:
    """Aggregates trades into seconds and labels each with its state."""

    def __init__(
        self,
        volume_window_seconds: int,
        minimum_volume_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if volume_window_seconds < 2:
            raise ValueError(
                "the quintile is relative to recent activity; a window of one second has no "
                "distribution to be relative to"
            )
        self._window = volume_window_seconds
        self._minimum = minimum_volume_observations
        self._now_ns = now_ns
        self._building: dict[tuple[str, str], SecondUnderConstruction] = {}
        self._volumes: dict[tuple[str, str], list] = {}
        self._last_close: dict[tuple[str, str], float] = {}
        self._states: dict[tuple[str, str], list] = {}
        self.standing = EncoderStanding()

    def observe_trade(self, venue_id: str, symbol: str, price: float, quantity: float, at_ns: int) -> None:
        """One trade. Seconds close when a trade for a later second arrives."""
        self.standing.trades_seen += 1
        key = (venue_id, symbol)
        second = at_ns // NANOSECONDS_PER_SECOND

        building = self._building.get(key)
        if building is None:
            self._building[key] = SecondUnderConstruction(second, price, quantity, 1)
            self.standing.symbols_tracked = len(self._building)
            return

        if second > building.second_ns:
            self._close_second(key, building)
            # Seconds with no trades between the two are still seconds, and the
            # flow was quiet in them. Skipping them would splice two
            # non-adjacent seconds into a transition that never happened.
            for empty in range(building.second_ns + 1, second):
                self._encode_empty(key, empty)
            self._building[key] = SecondUnderConstruction(second, price, quantity, 1)
            return

        building.close = price
        building.volume += quantity
        building.trades += 1

    def close_open_second(self, venue_id: str, symbol: str) -> None:
        """Close the second still being built, when the caller knows it is over."""
        key = (venue_id, symbol)
        building = self._building.pop(key, None)
        if building is not None:
            self._close_second(key, building)

    def states_for(self, venue_id: str, symbol: str, length: int) -> tuple:
        return tuple(self._states.get((venue_id, symbol), [])[-length:])

    def states_after(self, venue_id: str, symbol: str, after_second_ns: int, length: int) -> tuple:
        """This symbol's closed seconds newer than one already sent, newest last.

        The encoder holds a rolling window and the meter that reads it keeps its
        own, keyed by `second_ns`. Sending the whole window on every tick
        therefore restated up to 119 seconds the reader already had, to convey
        the one that was new -- and the restating is what made the new one
        unreachable. Measured on the live spine at 10:26 on 2026-08-26:

            trades_seen        541 a second
            seconds_encoded    105 a second   the real rate of new facts
            published       10,353 a second   the window, resent per trade batch
            flow-entropy-meter input_loss  158,902 order-flow-state, 210 a second

        99% of what went onto the bus was a restatement, and the bus was dropping
        210 a second of it into the one consumer that wanted it. Sending only
        what is new conveys strictly more, because nothing is lost carrying it.

        Capped at `length` for the same reason the window is: a symbol that went
        quiet for an hour and traded again must not dump an hour of seconds into
        one datagram burst. Past that cap the reader has a gap, which is the
        honest reading -- those seconds are older than the window it measures
        over anyway.
        """
        held = self._states.get((venue_id, symbol), [])
        fresh = [state for state in held if state.second_ns > after_second_ns]
        return tuple(fresh[-length:])

    def _close_second(self, key, building: SecondUnderConstruction) -> None:
        self._encode(key, building.second_ns, building.close, building.volume, building.trades)

    def _encode_empty(self, key, second_ns: int) -> None:
        self.standing.empty_seconds_encoded += 1
        self._encode(key, second_ns, self._last_close.get(key, 0.0), 0.0, 0)

    def _encode(self, key, second_ns: int, close: float, volume: float, trades: int) -> None:
        venue_id, symbol = key
        previous = self._last_close.get(key)
        if previous is None or close == previous:
            sign = 0
        else:
            sign = 1 if close > previous else -1
        if close > 0:
            self._last_close[key] = close

        quintile = self._quintile_of(key, volume)
        state = OrderFlowState(
            venue_id=venue_id, symbol=symbol, second_ns=second_ns, price_sign=sign,
            volume_quintile=quintile, close=close, volume=volume, trades=trades,
        )
        states = self._states.setdefault(key, [])
        states.append(state)
        del states[: max(0, len(states) - self._window * 4)]

        self.standing.seconds_encoded += 1
        self.standing.by_state[str(state)] = self.standing.by_state.get(str(state), 0) + 1

    def _quintile_of(self, key, volume: float) -> int:
        """The empirical CDF of volume over the trailing window, in fifths.

        Relative to recent activity rather than to a constant: a fixed threshold
        would call every second of a quiet symbol low-volume and every second of
        a busy one high, and the entropy of that sequence would describe the
        symbol's size rather than its flow.
        """
        volumes = self._volumes.setdefault(key, [])
        if len(volumes) < self._minimum:
            volumes.append(volume)
            del volumes[: max(0, len(volumes) - self._window)]
            self.standing.quintile_windows_short += 1
            return 3

        ordered = sorted(volumes)
        below = sum(1 for value in ordered if value <= volume)
        fraction = below / len(ordered)
        quintile = min(5, max(1, math.ceil(5 * fraction) or 1))

        volumes.append(volume)
        del volumes[: max(0, len(volumes) - self._window)]
        return quintile

    def release(self, venue_id: str, symbol: str) -> None:
        """Drop a symbol's history. T-3."""
        key = (venue_id, symbol)
        for table in (self._building, self._volumes, self._last_close, self._states):
            table.pop(key, None)
        self.standing.symbols_tracked = len(self._building)


def describe_encoding(encoder: OrderFlowStateEncoder) -> dict:
    return {
        "part_id": PART_ID,
        "state_count": STATE_COUNT,
        "trades_seen": encoder.standing.trades_seen,
        "seconds_encoded": encoder.standing.seconds_encoded,
        "empty_seconds_encoded": encoder.standing.empty_seconds_encoded,
        "seconds_before_the_volume_window_filled": encoder.standing.quintile_windows_short,
        "symbols_tracked": encoder.standing.symbols_tracked,
        "by_state": dict(sorted(encoder.standing.by_state.items())),
    }


def run_order_flow_state_encoder(
    encoder: OrderFlowStateEncoder, control_socket, read_trades, publish_states,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """Encode what traded, and publish only the seconds that have closed since.

    `read_trades` returns one entry per symbol touched this tick, carrying the
    second that symbol was last published through. A symbol trading ten times a
    second closes one second a second, so nine of those ten ticks find nothing
    new for it and publish nothing at all -- which is the whole difference
    between 10,353 messages a second and 105.
    """

    def tick() -> None:
        symbols = read_trades(encoder)
        publish_states(
            tuple(
                encoder.states_after(venue_id, symbol, published_through, length)
                for venue_id, symbol, published_through, length in symbols
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_encoding(encoder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    trades = Batch(read=context.bus.reader("market-data"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    publish_states = context.bus.publisher_for("order-flow-state")
    encoder = OrderFlowStateEncoder(
        volume_window_seconds=int(context.number("order_flow_volume_window")),
        minimum_volume_observations=int(context.number("order_flow_minimum_volume_observations")),
    )
    length = int(context.number("order_flow_state_length"))

    # The newest second already published, per symbol. A high-water mark rather
    # than a set: seconds only ever move forward, so one number per symbol says
    # everything a set of them would, and it does not grow with the run.
    published_through: dict[tuple[str, str], int] = {}
    # Before anything has been published for a symbol there is no mark, and every
    # second the encoder holds for it is new. -1 rather than 0 so a venue that
    # ever stamped the epoch is still newer than "nothing sent yet".
    NOTHING_PUBLISHED_YET = -1

    def read_trades(_encoder):
        books.payloads()
        touched = set()
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                # Upstox states a size on about a quarter of its LTP updates
                # and on no index at all (broker-market-data-bridge's own
                # docstring, measured on the 2026-09-04 tape). Order flow is a
                # statement about size, so a print with none is not observed --
                # a 0 would read as a print in which nothing changed hands.
                if trade.quantity is None:
                    continue
                encoder.observe_trade(trade.venue_id, trade.symbol, trade.price, trade.quantity, trade.venue_time_ns)
                touched.add((trade.venue_id, trade.symbol))
        return tuple(
            (
                venue_id,
                symbol,
                published_through.get((venue_id, symbol), NOTHING_PUBLISHED_YET),
                length,
            )
            for venue_id, symbol in sorted(touched)
        )

    def publish(state_tuples) -> None:
        flat = tuple(state for states in state_tuples for state in states)
        if not flat:
            return
        publish_states(flat)
        # Advanced only after the send: a mark moved first would silently skip
        # the seconds a failed publish never carried.
        for state in flat:
            key = (state.venue_id, state.symbol)
            published_through[key] = max(
                published_through.get(key, NOTHING_PUBLISHED_YET), state.second_ns
            )

    return run_order_flow_state_encoder(
        encoder=encoder,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_states=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
