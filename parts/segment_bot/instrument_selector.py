"""instrument-selector: which instrument actually realises an intent, and when none does.

An intent says "be long BTC by this much, with this risk, over this horizon". It
does not say *how*, and how is a real decision: a perpetual future, a dated
future, spot, or an option express the same view with different carry, different
liquidity and different ways of going wrong.

The selector prices each available instrument against the intent it is being
asked to carry:

- **Carry over the intent's own horizon.** A perpetual's funding is paid every
  settlement, a dated future's basis is paid once as it converges, and spot pays
  nothing. Which is cheapest is a function of the horizon, not a preference --
  and an hour and a month have opposite answers.
- **Whether the intent can be expressed at all.** A short cannot be expressed in
  spot without borrow. A convexity view needs an option. An instrument that
  cannot carry the intent is not a worse choice; it is not a choice.
- **Liquidity at the size actually being asked for.** The tightest instrument at
  one size is not the tightest at fifty times it.

**A segment that is not built is reported as not built, never skipped.** Options
and spot are deliberately empty at this stage (RL-050, RL-062), and if the best
instrument for an intent lives in one of them, this part says so and names it.
Quietly falling through to the futures instrument the system does have would make
the blueprint's build order invisible at exactly the moment it costs money, and
would show up in the record as a futures decision nobody made.

**A timed intent narrows the field rather than changing it.** An intent that must
be filled inside a window cannot use an instrument whose liquidity would take
longer than that window to absorb it, however cheap its carry.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instrument-selector"

PART_DECLARATION = PartDeclaration(
    part_id="instrument-selector",
    consumes=(
        "trade-intent", "market-data", "implied-vol-surface", "liquidity-grade", "timed-intent",
    ),
    produces=("instrument-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PERPETUAL_FUTURE = "perpetual-future"
DATED_FUTURE = "dated-future"
SPOT = "spot"
OPTION = "option"

# Which segment each instrument belongs to. The build order is futures first and
# the other two are honestly empty, so this table is what lets the part say
# "the right instrument is in a segment that is not built" instead of silently
# choosing a different one.
SEGMENT_OF = {
    PERPETUAL_FUTURE: "futures",
    DATED_FUTURE: "futures",
    SPOT: "spot",
    OPTION: "options",
}

CHOSEN = "chosen"
NOTHING_AVAILABLE = "no-instrument-is-listed-for-this-symbol"
NONE_CAN_CARRY_THE_INTENT = "no-listed-instrument-can-express-this-intent"
BEST_IS_IN_AN_UNBUILT_SEGMENT = "the-best-instrument-is-in-a-segment-that-is-not-built"
NONE_LIQUID_ENOUGH = "no-instrument-is-liquid-enough-at-this-size"
NONE_FAST_ENOUGH = "no-instrument-can-be-filled-inside-the-intent's-window"


@dataclass(frozen=True)
class ListedInstrument:
    """One way of expressing a view on a symbol, as the venue actually lists it."""

    venue_id: str
    symbol: str
    instrument_kind: str
    contract_symbol: str
    funding_rate_per_settlement: float | None
    settlements_per_day: float | None
    basis_fraction: float | None
    premium_fraction: float | None
    seconds_to_expiry: float | None
    supports_short: bool
    supports_convexity: bool
    round_trip_cost_fraction: float | None
    absorbable_quote: float | None
    seconds_to_fill: float | None

    @property
    def segment(self) -> str:
        return SEGMENT_OF.get(self.instrument_kind, "unknown")


@dataclass(frozen=True)
class InstrumentChoice:
    """Which instrument carries an intent, what it costs, and what was rejected."""

    venue_id: str
    symbol: str
    chosen: ListedInstrument | None
    total_cost_fraction: float | None
    carry_cost_fraction: float | None
    considered: int
    rejected: dict
    unbuilt_segment_would_have_won: str | None
    state: str
    reason: str
    chosen_at_ns: int
    # What the symbol last traded at when this choice was made. Carried because the
    # parts downstream have to size a position against a price and none of them
    # consumes market-data: the sizer's risk is a distance from an entry, and an
    # entry price that arrived separately would be a price from a different moment
    # than the choice it belongs to. None when nothing has traded yet.
    reference_price: float | None = None

    @property
    def is_actionable(self) -> bool:
        return self.chosen is not None


@dataclass
class SelectorStanding:
    intents_seen: int = 0
    chosen: int = 0
    by_kind: dict = field(default_factory=dict)
    by_refusal: dict = field(default_factory=dict)
    unbuilt_segment_wins: dict = field(default_factory=dict)
    largest_carry_avoided: float = 0.0


class InstrumentSelector:
    """Prices every listed instrument against the intent, and names what it cannot use."""

    def __init__(
        self,
        built_segments: tuple,
        maximum_cost_fraction: float,
        round_trip_cost_fraction: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not built_segments:
            raise ValueError(
                "a selector with no built segment can choose nothing; the built segments are "
                "a fact about this system's state and must be stated"
            )
        if not 0.0 < maximum_cost_fraction < 1.0:
            raise ValueError("the cost ceiling is a fraction of notional and must be inside (0, 1)")
        self._built_segments = tuple(built_segments)
        self._maximum_cost = maximum_cost_fraction
        # What a round trip costs on the perpetual this part registers from live
        # trades. None means it registers none, which is the right behaviour for a
        # caller that did not say what trading costs.
        self._round_trip_cost_fraction = round_trip_cost_fraction
        self._prices: dict[tuple[str, str], float] = {}
        self._now_ns = now_ns
        self._listed: dict[tuple[str, str], list] = {}
        self.standing = SelectorStanding()

    def observe_listed_instrument(self, instrument: ListedInstrument) -> None:
        key = (instrument.venue_id, instrument.symbol)
        listed = [
            existing
            for existing in self._listed.get(key, [])
            if existing.contract_symbol != instrument.contract_symbol
        ]
        listed.append(instrument)
        self._listed[key] = listed

    def carry_over(self, instrument: ListedInstrument, horizon_seconds: float) -> float | None:
        """What holding this instrument for the intent's horizon costs, as a fraction.

        Positive is a cost to a long. A perpetual pays per settlement; a dated
        future pays its basis as it converges, pro-rated over the fraction of its
        remaining life the intent occupies; spot pays nothing.
        """
        if instrument.instrument_kind == SPOT:
            return 0.0
        if instrument.instrument_kind == PERPETUAL_FUTURE:
            if instrument.funding_rate_per_settlement is None or instrument.settlements_per_day is None:
                return None
            settlements = horizon_seconds / 86400.0 * instrument.settlements_per_day
            return instrument.funding_rate_per_settlement * settlements
        if instrument.instrument_kind == DATED_FUTURE:
            if instrument.basis_fraction is None or not instrument.seconds_to_expiry:
                return None
            held = min(1.0, horizon_seconds / instrument.seconds_to_expiry)
            return instrument.basis_fraction * held
        if instrument.instrument_kind == OPTION:
            # An option's carry is the time value it loses while it is held. Time
            # value decays with the square root of remaining time, not linearly,
            # which is why a week held out of a month costs far less than a
            # quarter of the premium and a week held out of two costs most of it.
            if instrument.premium_fraction is None or not instrument.seconds_to_expiry:
                return None
            held = min(1.0, horizon_seconds / instrument.seconds_to_expiry)
            return instrument.premium_fraction * (1.0 - math.sqrt(1.0 - held))
        return None

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        """The last trade for a symbol, which is also how this part learns the
        symbol is tradeable at all.

        The perpetual is registered from the fact that it traded rather than from a
        catalogue this part does not consume: a symbol printing trades on a venue is
        a symbol that venue lists, and the round trip is the fee schedule the
        operator set. What is deliberately left None is funding -- unknown is not
        zero, and a carry cost invented here would make a perpetual look cheaper
        than a dated future nobody priced.
        """
        key = (venue_id, symbol)
        self._prices[key] = price
        if self._round_trip_cost_fraction is None:
            return
        if key not in self._listed:
            self._listed[key] = []
        if not any(i.instrument_kind == PERPETUAL_FUTURE for i in self._listed[key]):
            self._listed[key].append(
                ListedInstrument(
                    venue_id=venue_id,
                    symbol=symbol,
                    instrument_kind=PERPETUAL_FUTURE,
                    contract_symbol=symbol,
                    funding_rate_per_settlement=None,
                    settlements_per_day=None,
                    basis_fraction=None,
                    premium_fraction=None,
                    seconds_to_expiry=None,
                    supports_short=True,
                    supports_convexity=False,
                    round_trip_cost_fraction=self._round_trip_cost_fraction,
                    absorbable_quote=None,
                    seconds_to_fill=None,
                )
            )
            self.standing.instruments_seen = sum(len(v) for v in self._listed.values())

    def select(self, intent) -> InstrumentChoice:
        """One intent, priced against everything listed for its symbol."""
        self.standing.intents_seen += 1
        key = (intent.venue_id, intent.symbol)
        listed = self._listed.get(key, [])

        if not listed:
            return self._choice(
                intent, None, None, None, 0, {}, None, NOTHING_AVAILABLE,
                "nothing is listed for this symbol, so there is no way to express the intent",
            )

        rejected: dict[str, str] = {}
        priced = []

        for instrument in listed:
            refusal = self._cannot_carry(instrument, intent)
            if refusal is not None:
                rejected[instrument.contract_symbol] = refusal
                continue

            carry = self.carry_over(instrument, intent.horizon_seconds)
            if carry is None:
                rejected[instrument.contract_symbol] = (
                    "its carry could not be priced"
                    + (
                        "; an option needs its premium from the implied-vol surface"
                        if instrument.instrument_kind == OPTION
                        else ""
                    )
                )
                continue

            cost = instrument.round_trip_cost_fraction
            if cost is None:
                rejected[instrument.contract_symbol] = "its round-trip cost is not known"
                continue

            signed_carry = (
                carry
                if intent.is_long or instrument.instrument_kind == OPTION
                else -carry
            )
            priced.append((cost + max(0.0, signed_carry), signed_carry, instrument))

        if not priced:
            return self._choice(
                intent, None, None, None, len(listed), rejected, None,
                NONE_CAN_CARRY_THE_INTENT,
                f"all {len(listed)} listed instrument(s) were rejected: "
                + "; ".join(f"{name} -- {why}" for name, why in sorted(rejected.items())),
            )

        priced.sort(key=lambda entry: entry[0])
        best_cost, best_carry, best = priced[0]

        # The best instrument may live in a segment this system has not built.
        # Saying so is the point: falling through to the futures instrument that
        # does exist would record a decision nobody made.
        if best.segment not in self._built_segments:
            self.standing.unbuilt_segment_wins[best.segment] = (
                self.standing.unbuilt_segment_wins.get(best.segment, 0) + 1
            )
            built = [entry for entry in priced if entry[2].segment in self._built_segments]
            if not built:
                return self._choice(
                    intent, None, None, None, len(listed), rejected, best.segment,
                    BEST_IS_IN_AN_UNBUILT_SEGMENT,
                    f"the cheapest way to carry this intent is {best.contract_symbol} at "
                    f"{best_cost:.3%}, and it is in the {best.segment} segment, which is not "
                    f"built. Nothing in a built segment can express it, so this reports as "
                    f"unbuilt rather than choosing something else",
                )
            best_cost, best_carry, best = built[0]
            unbuilt_note = (
                f"; a cheaper instrument exists in the unbuilt {priced[0][2].segment} segment "
                f"at {priced[0][0]:.3%}, which is the cost of the build order rather than of "
                f"this decision"
            )
            self.standing.largest_carry_avoided = max(
                self.standing.largest_carry_avoided, best_cost - priced[0][0]
            )
        else:
            unbuilt_note = ""

        if best_cost > self._maximum_cost:
            return self._choice(
                intent, None, None, None, len(listed), rejected, None, NONE_LIQUID_ENOUGH,
                f"the cheapest usable instrument is {best.contract_symbol} at {best_cost:.3%}, "
                f"past the {self._maximum_cost:.3%} this intent may spend to be expressed",
            )

        self.standing.chosen += 1
        self.standing.by_kind[best.instrument_kind] = (
            self.standing.by_kind.get(best.instrument_kind, 0) + 1
        )

        return self._choice(
            intent, best, best_cost, best_carry, len(listed), rejected,
            priced[0][2].segment if priced[0][2].segment not in self._built_segments else None,
            CHOSEN,
            f"{best.contract_symbol} carries this intent at {best_cost:.3%} over its "
            f"{intent.horizon_seconds:.0f}s horizon "
            + (
                f"(carry {best_carry:+.3%}, cost {best.round_trip_cost_fraction:.3%})"
                if best.round_trip_cost_fraction is not None
                else ""
            )
            + f", the cheapest of {len(priced)} usable instrument(s)"
            + unbuilt_note,
        )

    def _cannot_carry(self, instrument: ListedInstrument, intent) -> str | None:
        """Why this instrument cannot express this intent, or None if it can."""
        if not intent.is_long and not instrument.supports_short:
            return "it cannot be sold short"
        if getattr(intent, "needs_convexity", False) and not instrument.supports_convexity:
            return "it cannot express a convexity view"
        if (
            instrument.absorbable_quote is not None
            and instrument.absorbable_quote < intent.notional_quote
        ):
            return (
                f"it can absorb {instrument.absorbable_quote:.0f} against the "
                f"{intent.notional_quote:.0f} asked for"
            )
        window = getattr(intent, "fill_within_seconds", None)
        if (
            window is not None
            and instrument.seconds_to_fill is not None
            and instrument.seconds_to_fill > window
        ):
            return (
                f"it would take {instrument.seconds_to_fill:.0f}s to fill against a "
                f"{window:.0f}s window"
            )
        if (
            instrument.instrument_kind == DATED_FUTURE
            and instrument.seconds_to_expiry is not None
            and instrument.seconds_to_expiry < intent.horizon_seconds
        ):
            return (
                f"it expires in {instrument.seconds_to_expiry:.0f}s, inside the intent's "
                f"{intent.horizon_seconds:.0f}s horizon"
            )
        return None

    def _choice(
        self, intent, chosen, cost, carry, considered, rejected, unbuilt, state, reason
    ) -> InstrumentChoice:
        if state != CHOSEN:
            self.standing.by_refusal[state] = self.standing.by_refusal.get(state, 0) + 1
        return InstrumentChoice(
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            chosen=chosen,
            total_cost_fraction=cost,
            carry_cost_fraction=carry,
            considered=considered,
            rejected=dict(rejected),
            unbuilt_segment_would_have_won=unbuilt,
            state=state,
            reason=reason,
            chosen_at_ns=self._now_ns(),
            reference_price=self._prices.get((intent.venue_id, intent.symbol)),
        )


def describe_instrument_selection(selector: InstrumentSelector) -> dict:
    return {
        "part_id": PART_ID,
        "built_segments": list(selector._built_segments),
        "intents_seen": selector.standing.intents_seen,
        "chosen": selector.standing.chosen,
        "chosen_by_kind": dict(sorted(selector.standing.by_kind.items())),
        "refused_by_reason": dict(sorted(selector.standing.by_refusal.items())),
        "times_an_unbuilt_segment_held_the_best_instrument": dict(
            sorted(selector.standing.unbuilt_segment_wins.items())
        ),
        "largest_cost_paid_for_the_build_order": selector.standing.largest_carry_avoided,
        "symbols_with_listed_instruments": len(selector._listed),
    }


def run_instrument_selector(
    selector: InstrumentSelector, control_socket, read_intents_and_instruments,
    publish_choices, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        intents = read_intents_and_instruments(selector)
        publish_choices(tuple(selector.select(intent) for intent in intents))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Which segments are built is a fact about this system, not a market fact: futures
    is built and spot and options are declared and honestly empty (RL-050, RL-062).
    The selector needs it so that when an unbuilt segment would have been the better
    instrument it can say so rather than silently choosing the second best --
    `unbuilt_segment_would_have_won` is how that becomes visible.
    """
    from runtime.input_assembly import Batch

    intents = Batch(read=context.bus.reader("trade-intent"))
    surfaces = Batch(read=context.bus.reader("implied-vol-surface"))
    grades = Batch(read=context.bus.reader("liquidity-grade"))
    timed = Batch(read=context.bus.reader("timed-intent"))
    trades = Batch(read=context.bus.reader("market-data"))
    publish_choices = context.bus.publisher_for("instrument-choice")

    def read_intents_and_instruments(selector):
        # Everything that describes what could be traded arrives as its own type;
        # the selector holds them and the intents are what ask a question.
        for instrument in list(surfaces.payloads()) + list(grades.payloads()):
            selector.observe_listed_instrument(instrument)
        for trade in trades.payloads():
            selector.observe_price(trade.venue_id, trade.symbol, trade.price)
        timed.payloads()
        return intents.payloads()

    return run_instrument_selector(
        selector=InstrumentSelector(
            built_segments=(context.setting("segment_id").value,),
            maximum_cost_fraction=context.number("instrument_maximum_cost_fraction"),
            # A perpetual's round trip is two crossings of the spread at the taker
            # rate. Nothing lists instruments for this system yet, so the venue's
            # own perpetual is registered from the trades that arrive and priced at
            # the fee schedule the operator set.
            round_trip_cost_fraction=2 * context.number("taker_fee_rate"),
        ),
        control_socket=context.control_socket,
        read_intents_and_instruments=read_intents_and_instruments,
        publish_choices=publish_choices,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
