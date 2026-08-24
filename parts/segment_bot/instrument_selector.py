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
from collections import Counter
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.price_staleness import ObservedPrice, PriceStalenessEstimator
from runtime.part_process import run_part
from runtime.trading_types import DATED_FUTURE, OPTION, PERPETUAL_FUTURE, SPOT

PART_ID = "instrument-selector"

PART_DECLARATION = PartDeclaration(
    part_id="instrument-selector",
    consumes=(
        "trade-intent", "market-data", "implied-vol-surface", "liquidity-grade", "timed-intent",
        "symbol-universe",
    ),
    produces=("instrument-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

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
NO_REFERENCE_PRICE_HAS_EVER_ARRIVED = "this-symbol-has-never-printed-a-trade-here"
REFERENCE_PRICE_IS_TOO_OLD = "this-symbol's-last-price-is-too-old-to-size-against"


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
    # than the choice it belongs to. None when nothing has traded yet, and None
    # when what did trade is older than this part was told to believe -- a price
    # withheld reads to every reader downstream as the absence it is, rather than
    # as a number.
    reference_price: float | None = None
    # When that price printed, by the venue's own clock. Carried separately from
    # `chosen_at_ns` because they are different moments, and conflating them is the
    # defect this field closes: a choice made now can only carry a price from
    # whenever the symbol last traded, and on the live run of 2026-08-23 that gap
    # reached fifty-six minutes while every message about it looked current.
    reference_price_observed_at_ns: int | None = None

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
    # How many listings from the venue's own universe became instruments this part
    # can price, and why each of the rest did not. Counted because a selector that
    # priced nothing looks identical to one nobody asked anything of, and the
    # difference is the whole diagnosis (Rule 8).
    listings_registered: int = 0
    listings_skipped: Counter = field(default_factory=Counter)


class InstrumentSelector:
    """Prices every listed instrument against the intent, and names what it cannot use."""

    def __init__(
        self,
        built_segments: tuple,
        maximum_cost_fraction: float,
        round_trip_cost_fraction: float | None = None,
        price_staleness: PriceStalenessEstimator | None = None,
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
        # How old a symbol's last print may be and still be a price this part will
        # let a position be sized against -- asked per symbol rather than held as a
        # number here, because BTCUSDT tolerates fifteen seconds where ETHUSDT
        # tolerates one and a single bound would be wrong for both. None means the
        # caller stated no bound, and this part does not invent one (RL-061).
        self._price_staleness = price_staleness
        # Price and the moment it printed, together, because they are one fact.
        self._prices: dict[tuple[str, str], ObservedPrice] = {}
        self._now_ns = now_ns
        # The moment and the symbol the choice being built is about, so the price
        # handed out is judged against the same instant, and the same symbol, the
        # refusal was.
        self._deciding_at_ns = now_ns()
        self._deciding_about: tuple[str, str] = ("", "")
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

    def observe_price(self, venue_id: str, symbol: str, price: float, observed_at_ns: int) -> None:
        """The last trade for a symbol, kept so a choice carries the price it was
        made at, and when that price printed.

        `observed_at_ns` has no default on purpose. A caller that omitted it would
        be storing a price with no age, which is exactly the shape that priced an
        ENAUSDT order fifty-six minutes late on 2026-08-23.

        It registers no instrument. What contracts a venue lists and what they
        cost to hold is the venue's own statement, and it arrives on
        `symbol-universe`; a symbol printing trades is evidence that some contract
        exists, not a statement of its terms. Inferring one here is what left every
        perpetual with an unpriceable carry.
        """
        self._prices[(venue_id, symbol)] = ObservedPrice(price=price, observed_at_ns=observed_at_ns)
        if self._price_staleness is not None:
            self._price_staleness.observe_price(venue_id, symbol, price, observed_at_ns)

    def observe_listed_symbol(self, listed) -> None:
        """One entry of `symbol-universe`: a contract the venue lists, on its terms.

        Only a perpetual is registered, and only when the venue declared both
        halves of its funding -- the rate it last charged and how often it charges
        one. Either half missing means the carry cannot be priced, and an
        unregistered instrument is refused by name at selection
        (`no-instrument-is-listed-for-this-symbol`) rather than silently priced as
        free. Measured 2026-08-22: Binance declares an interval for 740 of its 872
        listed symbols, so this is a real state and not a defensive branch.

        A dated future is deliberately not registered even though its kind is
        known: its carry is its basis, nothing published to this part carries one,
        and registering it with `basis_fraction=None` would add a listing that
        could only ever be rejected. What is not priceable is not listed here, and
        the count of what was skipped is what says so.

        Spot and options are not registered either, for a different reason: those
        segments are honestly empty (RL-050, RL-062). When they are built their
        instruments will arrive the same way, and `unbuilt_segment_would_have_won`
        is what reports the cost of their absence until then.
        """
        if self._round_trip_cost_fraction is None:
            self.standing.listings_skipped["no round-trip cost was set for this selector"] += 1
            return
        if listed.instrument_kind != PERPETUAL_FUTURE:
            self.standing.listings_skipped[
                f"kind {listed.instrument_kind or 'unrecognised'} is not priced by this part yet"
            ] += 1
            return
        if listed.funding_rate_per_settlement is None or listed.funding_settlements_per_day is None:
            self.standing.listings_skipped[
                "the venue declared no funding rate or no settlement interval"
            ] += 1
            return
        self.observe_listed_instrument(
            ListedInstrument(
                venue_id=listed.venue_id,
                symbol=listed.symbol,
                instrument_kind=PERPETUAL_FUTURE,
                contract_symbol=listed.symbol,
                funding_rate_per_settlement=listed.funding_rate_per_settlement,
                settlements_per_day=listed.funding_settlements_per_day,
                basis_fraction=None,
                premium_fraction=None,
                seconds_to_expiry=None,
                # A linear perpetual is short-sellable and carries no convexity.
                # Both are properties of the contract rather than of this venue.
                supports_short=True,
                supports_convexity=False,
                # Two crossings of the spread at the taker rate the operator set.
                # The venue owns the funding above; the operator owns this.
                round_trip_cost_fraction=self._round_trip_cost_fraction,
                absorbable_quote=None,
                seconds_to_fill=None,
            )
        )
        self.standing.listings_registered += 1

    def select(self, intent, now_ns: int | None = None) -> InstrumentChoice:
        """One intent, priced against everything listed for its symbol.

        `now_ns` is when the decision is being made, which is what the reference
        price's age is measured against. It defaults to this part's clock so the
        live tick loop reads unchanged; a caller replaying the tape passes the
        moment it is replaying, because a price is stale relative to the decision
        and not to the wall clock of whoever is asking.
        """
        self.standing.intents_seen += 1
        at = self._now_ns() if now_ns is None else now_ns
        key = (intent.venue_id, intent.symbol)
        self._deciding_at_ns, self._deciding_about = at, key
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

        # Everything about the instrument holds. What is left is whether this part
        # knows what the symbol is worth right now, and a position cannot be sized
        # against a price nobody can date. Checked last so the refusal can name the
        # instrument that would otherwise have been chosen.
        refusal = self._price_refusal(key, at)
        if refusal is not None:
            state, why = refusal
            return self._choice(
                intent, None, None, None, len(listed), rejected, None, state,
                f"{best.contract_symbol} would carry this intent at {best_cost:.3%}, and {why}",
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

    def _believable_price(self, observed: "ObservedPrice | None") -> float | None:
        """The price to hand downstream, or None where it is too old to hand over.

        The time it printed still travels with the choice either way: a reader that
        can see when the last price was is being told why there is no number, which
        is a different thing from being told nothing.
        """
        if observed is None or self._price_staleness is None:
            return None if observed is None else observed.price
        bound = self._price_staleness.believable_age_seconds(*self._deciding_about)
        if observed.age_seconds(self._deciding_at_ns) > bound.value:
            return None
        return observed.price

    def _price_refusal(self, key: tuple[str, str], at_ns: int) -> tuple[str, str] | None:
        """Why this symbol cannot be priced right now, or None if it can.

        Only reached when a bound was given. Told nothing about staleness, this
        part refuses nothing on age -- which is the behaviour every caller had
        before the bound existed, and is why the bound is where the provenance is.
        """
        if self._price_staleness is None:
            return None
        observed = self._prices.get(key)
        if observed is None:
            return (
                NO_REFERENCE_PRICE_HAS_EVER_ARRIVED,
                "no trade has ever printed for this symbol here, so there is no price to "
                "size a position against. Never seen is not a stale price and not a zero",
            )
        age = observed.age_seconds(at_ns)
        bound = self._price_staleness.believable_age_seconds(*key)
        if age > bound.value:
            return (
                REFERENCE_PRICE_IS_TOO_OLD,
                f"the last trade this part saw for the symbol printed {age:.0f}s ago, past the "
                f"{bound.value:.2f}s this symbol's own moves say a price may be believed "
                f"({bound.reason}). Sizing against it would put the risk a fixed distance from "
                f"a price the market has left",
            )
        return None

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
        observed = self._prices.get((intent.venue_id, intent.symbol))
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
            reference_price=self._believable_price(observed),
            reference_price_observed_at_ns=None if observed is None else observed.observed_at_ns,
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
        "listings_registered": selector.standing.listings_registered,
        "listings_skipped": dict(sorted(selector.standing.listings_skipped.items())),
        # What this part currently believes about how long each symbol's price is
        # worth acting on. Reported because a refusal that cannot be seen from
        # outside is indistinguishable from an input that never arrived, and a
        # bound that quietly collapsed to its floor would stop every trade while
        # looking exactly like a market nobody wanted to trade.
        "price_staleness": (
            None if selector._price_staleness is None else selector._price_staleness.describe()
        ),
    }


def run_instrument_selector(
    selector: InstrumentSelector, control_socket, read_intents_and_instruments,
    publish_choices, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
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
    universe = Batch(read=context.bus.reader("symbol-universe"))
    publish_choices = context.bus.publisher_for("instrument-choice")

    def read_intents_and_instruments(selector):
        # Everything that describes what could be traded arrives as its own type;
        # the selector holds them and the intents are what ask a question.
        #
        # symbol-universe is the venue's own listing and is republished whole on
        # every catalogue read, so a funding rate that changed at settlement
        # arrives as a replacement for the instrument rather than as a second one:
        # `observe_listed_instrument` keys on the contract symbol and overwrites.
        for listed in universe.payloads():
            selector.observe_listed_symbol(listed)
        for instrument in list(surfaces.payloads()) + list(grades.payloads()):
            selector.observe_listed_instrument(instrument)
        for trade in trades.payloads():
            # The venue's own time for the print, not this part's clock: how old a
            # price is has to be measured from when the market made it.
            selector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
            )
        timed.payloads()
        return intents.payloads()

    return run_instrument_selector(
        selector=InstrumentSelector(
            built_segments=(context.setting("segment_id").value,),
            maximum_cost_fraction=context.number("instrument_maximum_cost_fraction"),
            # A perpetual's round trip is two crossings of the spread at the taker
            # rate. The venue states what holding the contract costs; what
            # trading it costs is the fee schedule the operator set, so the two
            # halves of an instrument's cost come from the two sides that own
            # them.
            round_trip_cost_fraction=2 * context.number("taker_fee_rate"),
            # What makes a stale price material is the same threshold that makes a
            # cost material, so the two come from one setting rather than from two
            # that could disagree.
            price_staleness=PriceStalenessEstimator(
                materiality_fraction=2 * context.number("taker_fee_rate"),
                anchor_seconds=context.number("reference_price_move_anchor_seconds"),
                quantile=context.number("reference_price_move_quantile"),
                window=int(context.number("reference_price_move_window")),
                observations_needed=int(
                    context.number("reference_price_move_observations_needed")
                ),
                prior_one_second_move=context.number("reference_price_prior_one_second_move"),
                minimum_age_seconds=context.number("reference_price_minimum_age_seconds"),
                maximum_age_seconds=context.number("reference_price_maximum_age_seconds"),
            ),
        ),
        control_socket=context.control_socket,
        read_intents_and_instruments=read_intents_and_instruments,
        publish_choices=publish_choices,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
