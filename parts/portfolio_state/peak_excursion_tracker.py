"""peak-excursion-tracker: the best and worst unrealised points a position reached (RL-042)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.lot_book_checkpoint import book_key_of, book_key_text
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG

PART_ID = "peak-excursion-tracker"
CHECKPOINT_COMPONENT = "excursion"

PART_DECLARATION = PartDeclaration(
    part_id="peak-excursion-tracker",
    consumes=("position", "market-data", "cost-basis"),
    produces=("peak-excursion", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class PeakExcursion:
    """How far a position went right and wrong before it was closed."""

    venue_id: str
    symbol: str
    best_unrealised: float
    worst_unrealised: float
    best_price: float
    worst_price: float
    current_unrealised: float
    samples: int
    observed_at_ns: int


@dataclass
class ExcursionStanding:
    prices_seen: int = 0
    positions_tracked: int = 0
    positions_closed: int = 0
    without_cost_basis: int = 0
    # Not counters: what happened to the checkpoint at start. A part that came
    # back holding nothing and one whose checkpoint could not be read are
    # different facts, and only the second is a fault (Rule 8).
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


class PeakExcursionTracker:
    """Marks each open position to market and keeps its extremes.

    The extremes are the point (RL-042): a trade that closed flat after being 5%
    up and a trade that never moved look identical in realised profit, and only
    one of them was a missed exit. Nothing else records that difference.

    A position with no cost basis is counted, not estimated: an excursion
    measured from a guessed entry is a number that reads exactly like a real one.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._last_observed_at_ns: dict[tuple[str, str], int] = {}
        self._basis: dict[tuple[str, str], tuple[float, float, str]] = {}
        self._extremes: dict[tuple[str, str], list[float]] = {}
        self._samples: dict[tuple[str, str], int] = {}
        # What a checkpoint is written against. Not `prices_seen`: that counts
        # every print across the whole tape, most of which carry no open
        # position and never touch `_extremes` at all -- keying a checkpoint
        # schedule on it would fsync at the tape's rate rather than at the rate
        # this part's own state actually changes.
        self._basis_observations = 0
        self.standing = ExcursionStanding()

    @property
    def checkpointable_observations(self) -> int:
        return self._basis_observations

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        if position.is_flat:
            self._basis.pop(key, None)
            self._extremes.pop(key, None)
            self._samples.pop(key, None)
            self.standing.positions_closed += 1
        else:
            self._basis[key] = (
                position.average_entry_price,
                abs(position.quantity),
                position.direction,
            )
        self.standing.positions_tracked = len(self._basis)

    def observe_price(
        self, venue_id: str, symbol: str, price: float, observed_at_ns: int
    ) -> PeakExcursion | None:
        """One print against an open position's cost basis.

        `observed_at_ns` is the venue's own time for the print and has no default.
        Until 2026-08-24 the excursion was stamped with this part's clock instead,
        so a price that reached it late -- the bus drops rather than blocks, so
        prints do arrive late -- was recorded as the market of the moment it was
        read. Eight and a half million excursions are the record stop placement and
        exit timing are learned from, and every one of them dated by when it was
        processed rather than when it happened is a record of a market that never
        existed in that order.
        """
        self.standing.prices_seen += 1
        key = (venue_id, symbol)
        basis = self._basis.get(key)
        if basis is None:
            self.standing.without_cost_basis += 1
            return None

        entry, quantity, direction = basis
        unrealised = (price - entry) * quantity * (1 if direction == LONG else -1)
        extremes = self._extremes.setdefault(key, [unrealised, unrealised, price, price])
        if unrealised > extremes[0]:
            extremes[0], extremes[2] = unrealised, price
        if unrealised < extremes[1]:
            extremes[1], extremes[3] = unrealised, price
        self._samples[key] = self._samples.get(key, 0) + 1
        self._last_observed_at_ns[key] = observed_at_ns
        self._basis_observations += 1

        return PeakExcursion(
            venue_id=venue_id,
            symbol=symbol,
            best_unrealised=extremes[0],
            worst_unrealised=extremes[1],
            best_price=extremes[2],
            worst_price=extremes[3],
            current_unrealised=unrealised,
            samples=self._samples[key],
            observed_at_ns=observed_at_ns,
        )

    def read(self, venue_id: str, symbol: str) -> PeakExcursion | None:
        key = (venue_id, symbol)
        extremes = self._extremes.get(key)
        if extremes is None:
            return None
        return PeakExcursion(
            venue_id, symbol, extremes[0], extremes[1], extremes[2], extremes[3],
            extremes[0], self._samples.get(key, 0),
            # When the market last printed for this position, not when the reader
            # happened to ask: an excursion describes the market, and dating it by
            # the moment it was read would make a position nobody is pricing look
            # freshly measured.
            self._last_observed_at_ns.get(key, self._now_ns()),
        )

    # -- what survives a restart ----------------------------------------------
    #
    # Held in memory alone until 2026-08-29. RL-042 is the whole point of this
    # part -- "the extremes are the point" -- and a restart discarded them:
    # every position open across a restart lost its true lifetime best/worst
    # and started re-measuring from whatever it was right after. Measured this
    # session: five spine restarts in one sitting, each one a silent reset for
    # every open position's excursion.
    #
    # This is also the fix for a second, separate symptom: `position-close-
    # detector` only writes its own checkpoint on a fill (lots genuinely only
    # change on a fill, so that part of its cadence is correct), so the
    # `excursion` it relays there can be stale by however long since the last
    # fill on any position -- measured live, 573 seconds -- while the board's
    # live price and unrealised are always fresh. The board now reads this
    # part's own checkpoint for open positions instead of that relay, and this
    # checkpoint is scheduled far more often (every real excursion update, not
    # every fill), so a reader gets this part's own current answer rather than
    # a downstream copy of an old one.

    def read_checkpoint_state(self) -> dict:
        """Every open position's basis and extremes, so a restart does not start blind."""
        return {
            "basis": {
                book_key_text(key): {
                    "entry": entry, "quantity": quantity, "direction": direction,
                }
                for key, (entry, quantity, direction) in self._basis.items()
            },
            "extremes": {
                book_key_text(key): list(values) for key, values in self._extremes.items()
            },
            "samples": {book_key_text(key): count for key, count in self._samples.items()},
            "last_observed_at_ns": {
                book_key_text(key): at_ns for key, at_ns in self._last_observed_at_ns.items()
            },
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Refill every position's basis and extremes. Returns how many came back."""
        self._basis = {
            book_key_of(text): (float(v["entry"]), float(v["quantity"]), str(v["direction"]))
            for text, v in (state.get("basis") or {}).items()
        }
        self._extremes = {
            book_key_of(text): [float(x) for x in values]
            for text, values in (state.get("extremes") or {}).items()
        }
        self._samples = {
            book_key_of(text): int(count) for text, count in (state.get("samples") or {}).items()
        }
        self._last_observed_at_ns = {
            book_key_of(text): int(at_ns)
            for text, at_ns in (state.get("last_observed_at_ns") or {}).items()
        }
        self.standing.positions_tracked = len(self._basis)
        self.standing.restored_symbols = len(self._basis)
        return len(self._basis)


def describe_excursions(tracker: PeakExcursionTracker) -> dict:
    return {
        "part_id": PART_ID,
        "prices_seen": tracker.standing.prices_seen,
        "positions_tracked": tracker.standing.positions_tracked,
        "positions_closed": tracker.standing.positions_closed,
        "prices_without_cost_basis": tracker.standing.without_cost_basis,
        "restored_symbols": tracker.standing.restored_symbols,
        "checkpoint_verdict": tracker.standing.checkpoint_verdict,
    }


def run_peak_excursion_tracker(
    tracker: PeakExcursionTracker, control_socket, read_positions_and_prices, publish_excursion,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        """One batch of excursions per tick, not one call per price.

        A publisher takes an iterable of payloads; handed a single frozen
        dataclass it raises rather than publishing, which killed this part on the
        first price that arrived for an open position.
        """
        positions, prices = read_positions_and_prices()
        for position in positions:
            tracker.observe_position(position)
        excursions = []
        for venue_id, symbol, price, observed_at_ns in prices:
            excursion = tracker.observe_price(venue_id, symbol, price, observed_at_ns)
            if excursion is not None:
                excursions.append(excursion)
        publish_excursion(tuple(excursions))
        if excursions and write_checkpoint is not None:
            write_checkpoint(tracker.checkpointable_observations)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_excursions(tracker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    How far each open position has gone in favour and against, measured against
    live prices as they arrive (RL-071). This is the input that makes a closed
    trade readable afterwards: without it, a trade that ran to +3% and closed at
    -1% and a trade that went straight to -1% are the same row.

    Every open position's basis and extremes survive a restart, since
    2026-08-29: beside the lot books and resting exits, under
    position_state_root, because it is the same class of fact -- what this
    part currently knows about a position it is watching.
    """
    import pathlib

    # `market-data` carries trades AND candles: venue-trade-stream-reader
    # publishes the first, ccxt-venue-reader the second, and both have always
    # declared it. This part wants trades and now says so, rather than assuming
    # the wire holds only what it happens to want -- a part that dies on an
    # unexpected shape is a part the wiring can kill.
    from runtime.durable_state import CheckpointSchedule, DurableStateStore, restore_and_arm_checkpoint
    from runtime.market_data_stream import trades_in
    from runtime.input_assembly import Batch

    positions = Batch(read=context.bus.reader("position"))
    trades = Batch(read=context.bus.reader("market-data"))
    bases = Batch(read=context.bus.reader("cost-basis"))
    publish_excursion = context.bus.publisher_for("peak-excursion")
    tracker = PeakExcursionTracker()
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        # Keyed on excursion updates, not the tape: a handful of open positions
        # priced continuously is a far lower rate than the whole market's print
        # stream, so this can afford to be frequent -- close to the fill-gated
        # checkpoint's own cadence would defeat the point of fixing it.
        CheckpointSchedule(int(context.number("peak_excursion_checkpoint_interval"))),
        PART_ID,
        CHECKPOINT_COMPONENT,
        tracker,
        {},
    )

    def read_positions_and_prices():
        # `cost-basis` is declared and drained, and this part measures against the
        # position's own average entry price rather than against it. The two agree
        # for a position built from fills this system made; they diverge for one
        # partly closed, where the lot book knows which lots are left and the
        # position's average does not. Using it here is a change to what an
        # excursion means, which is a decision for the part that owns the meaning
        # -- so the input is read and named, not silently half-applied.
        bases.payloads()
        prices = tuple(
            (trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)
            for trade in trades_in(trades.payloads())
        )
        return tuple(positions.payloads()), prices

    return run_peak_excursion_tracker(
        tracker=tracker,
        control_socket=context.control_socket,
        read_positions_and_prices=read_positions_and_prices,
        publish_excursion=publish_excursion,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
