"""intent-timing-gate: the last thing between a decision and the desk that fills it.

The bots each decided when their own setup should be entered. The arbiter then
combined opinions that may have been formed seconds apart, and the result is an
intent whose timing belongs to nobody. This part gives it one.

Three things it does that nothing upstream can:

- **It reconciles the bots' timings.** Two bots agreeing on direction can
  disagree on the moment, and the intent inherits neither. The strictest wins:
  if one wants to wait for a trigger, the intent waits, because acting now on a
  view that half its evidence says is early is the worst of both.
- **It stamps an expiry on everything.** An intent with no expiry is a trade
  taken later by reasoning that has aged out -- the single most reliable way an
  automated system takes a position nobody would take.
- **It checks the price has not already moved past the decision.** A conviction
  formed at one price is not the same conviction at a price two percent away, and
  the gate refuses rather than filling into the move that made the reasoning
  stale.

**A waiting intent is published, not held.** Whoever holds it needs to see the
trigger and the expiry, and an intent kept quietly inside this part would be
invisible to every board and every audit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import ENTER_NOW, STAND_DOWN, WAIT_FOR_TRIGGER
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import LONG, TimedIntent

PART_ID = "intent-timing-gate"

PART_DECLARATION = PartDeclaration(
    part_id="intent-timing-gate",
    consumes=("trade-intent", "market-data", "bull-entry-timing", "bear-entry-timing"),
    produces=("timed-intent", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ACT_NOW = "nothing-is-waiting-for-anything"
WAITING_ON_A_BOT = "a-contributing-bot-is-waiting-for-a-trigger"
PRICE_HAS_MOVED_PAST_THE_DECISION = "price-has-moved-past-where-this-was-decided"
NO_PRICE = "no-price-for-this-symbol"
A_BOT_STOOD_DOWN_ON_TIMING = "a-contributing-bot-stood-down-on-timing"


@dataclass
class GateStanding:
    intents_timed: int = 0
    acted_now: int = 0
    waiting: int = 0
    refused: int = 0
    expired: int = 0
    by_reason: dict = field(default_factory=dict)
    largest_drift_refused: float = 0.0


class IntentTimingGate:
    """Gives an intent a moment to act on and an expiry, or refuses it."""

    def __init__(
        self,
        validity_seconds: float,
        maximum_price_drift_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if validity_seconds <= 0:
            raise ValueError(
                "an intent with no expiry is a trade taken later by reasoning that has aged out"
            )
        if not 0.0 < maximum_price_drift_fraction < 1.0:
            raise ValueError(
                "the drift ceiling is a fraction of the decision price and must be inside (0, 1)"
            )
        self._validity_seconds = validity_seconds
        self._maximum_drift = maximum_price_drift_fraction
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._decision_prices: dict[tuple[str, str], float] = {}
        self._bot_timings: dict[tuple[str, str, str], object] = {}
        self.standing = GateStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        self._prices[(venue_id, symbol)] = price

    def observe_bot_timing(self, bot: str, timing) -> None:
        self._bot_timings[(bot, timing.venue_id, timing.symbol)] = timing

    def record_decision_price(self, venue_id: str, symbol: str, price: float) -> None:
        """The price the conviction was formed at, so drift away from it can be seen."""
        self._decision_prices[(venue_id, symbol)] = price

    def gate(self, intent) -> TimedIntent | None:
        """One intent, given a moment and an expiry, or refused with a reason."""
        self.standing.intents_timed += 1
        key = (intent.venue_id, intent.symbol)

        price = self._prices.get(key)
        if price is None:
            return self._refuse(intent, NO_PRICE, "no price has arrived for this symbol")

        decided_at = self._decision_prices.get(key)
        if decided_at is not None and decided_at > 0:
            drift = abs(price - decided_at) / decided_at
            if drift > self._maximum_drift:
                self.standing.largest_drift_refused = max(
                    self.standing.largest_drift_refused, drift
                )
                return self._refuse(
                    intent, PRICE_HAS_MOVED_PAST_THE_DECISION,
                    f"price has moved {drift:.2%} from the {decided_at:.8g} this was decided "
                    f"at, past the {self._maximum_drift:.2%} that keeps a conviction the same "
                    f"conviction; filling here would be trading the move that made the "
                    f"reasoning stale",
                )

        timings = [
            self._bot_timings.get((bot, intent.venue_id, intent.symbol))
            for bot in intent.contributing_bots
        ]
        present = [timing for timing in timings if timing is not None]

        if any(timing.action == STAND_DOWN for timing in present):
            return self._refuse(
                intent, A_BOT_STOOD_DOWN_ON_TIMING,
                "a bot that contributed to this intent has since stood down on timing",
            )

        waiting = [timing for timing in present if timing.action == WAIT_FOR_TRIGGER]
        if waiting:
            # The strictest wins. Acting now on a view half of whose evidence
            # says it is early is the worst of both.
            trigger = (
                min(timing.trigger_price for timing in waiting)
                if intent.side == LONG
                else max(timing.trigger_price for timing in waiting)
            )
            self.standing.waiting += 1
            return self._timed(
                intent, False, trigger, WAITING_ON_A_BOT,
                f"{len(waiting)} of {len(present)} contributing bot(s) want to wait; the "
                f"strictest applies, so this waits for {trigger:.8g} and expires in "
                f"{self._validity_seconds:g}s",
            )

        self.standing.acted_now += 1
        return self._timed(
            intent, True, price, ACT_NOW,
            f"{len(present)} contributing bot(s) want to act now at {price:.8g}; this expires "
            f"in {self._validity_seconds:g}s so it cannot be filled later by reasoning that "
            f"has aged out",
        )

    def has_expired(self, timed: TimedIntent) -> bool:
        expired = timed.has_expired(self._now_ns())
        if expired:
            self.standing.expired += 1
        return expired

    def _timed(self, intent, act_now, trigger, waited_for, reason) -> TimedIntent:
        self.standing.by_reason[waited_for] = self.standing.by_reason.get(waited_for, 0) + 1
        return TimedIntent(
            intent=intent,
            act_now=act_now,
            trigger_price=trigger,
            valid_until_ns=self._now_ns() + int(self._validity_seconds * 1e9),
            waited_for=waited_for,
            reason=reason,
            timed_at_ns=self._now_ns(),
        )

    def _refuse(self, intent, reason_code, reason) -> TimedIntent:
        self.standing.refused += 1
        self.standing.by_reason[reason_code] = self.standing.by_reason.get(reason_code, 0) + 1
        return TimedIntent(
            intent=intent,
            act_now=False,
            trigger_price=None,
            valid_until_ns=None,
            waited_for=reason_code,
            reason=reason,
            timed_at_ns=self._now_ns(),
        )


def describe_timing(gate: IntentTimingGate) -> dict:
    return {
        "part_id": PART_ID,
        "intents_timed": gate.standing.intents_timed,
        "acted_now": gate.standing.acted_now,
        "waiting_for_a_trigger": gate.standing.waiting,
        "refused": gate.standing.refused,
        "expired_before_acting": gate.standing.expired,
        "by_reason": dict(sorted(gate.standing.by_reason.items())),
        "largest_price_drift_refused": gate.standing.largest_drift_refused,
    }


def run_intent_timing_gate(
    gate: IntentTimingGate, control_socket, read_intents_and_timings, publish_timed,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        intents = read_intents_and_timings(gate)
        publish_timed(tuple(gate.gate(intent) for intent in intents))

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

    The price an intent was decided at is the latest trade for its symbol
    when the intent arrives; the gate then holds or passes it against the
    bots' own timing and the price now.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    intents = Batch(read=context.bus.reader("trade-intent"))
    trades = Batch(read=context.bus.reader("market-data"))
    bull_timings = Batch(read=context.bus.reader("bull-entry-timing"))
    bear_timings = Batch(read=context.bus.reader("bear-entry-timing"))
    publish_timed = context.bus.publisher_for("timed-intent")
    gate = IntentTimingGate(
        validity_seconds=context.number("intent_timing_validity"),
        maximum_price_drift_fraction=context.number("intent_timing_maximum_price_drift"),
    )
    prices: dict[tuple[str, str], float] = {}

    def read_intents_and_timings(_gate):
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                prices[(trade.venue_id, trade.symbol)] = trade.price
                gate.observe_price(trade.venue_id, trade.symbol, trade.price)
        for timing in bull_timings.payloads():
            gate.observe_bot_timing(timing.bot, timing)
        for timing in bear_timings.payloads():
            gate.observe_bot_timing(timing.bot, timing)
        actionable = []
        for intent in intents.payloads():
            if not intent.is_actionable:
                continue
            price = prices.get((intent.venue_id, intent.symbol))
            if price is not None:
                gate.record_decision_price(intent.venue_id, intent.symbol, price)
            actionable.append(intent)
        return tuple(actionable)

    def publish(timed) -> None:
        kept = tuple(item for item in timed if item is not None)
        if kept:
            publish_timed(kept)

    return run_intent_timing_gate(
        gate=gate,
        control_socket=context.control_socket,
        read_intents_and_timings=read_intents_and_timings,
        publish_timed=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
