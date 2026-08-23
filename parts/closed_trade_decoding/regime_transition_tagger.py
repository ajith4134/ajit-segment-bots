"""regime-transition-tagger: was this the trade that was decided on.

A trade entered in a trending market and closed in a choppy one was not the trade the
setup was chosen for. Learning from it as though it were teaches the wrong lesson
twice: the setup gets blamed for a market that no longer existed, and the regime
detector gets no feedback at all.

The tagging is deliberately conservative in three ways, because a tagger that finds a
transition everywhere makes every inconvenient loss excusable:

- **A change is only a change if it persisted.** A single-tick flicker back and forth
  is the regime detector being noisy, not the market changing character, so a
  transition must hold for a minimum span before it counts.
- **How much of the trade sat in the new regime is measured**, not just whether a
  change happened. A trade that spent 5% of its life in the new regime was
  essentially the trade that was decided on; one that spent 80% was not.
- **A transition is a fact, not an excuse.** The tag says the conditions changed. It
  does not say the loss was unavoidable, and the classifier that reads it weights it
  by the share rather than treating any change as exculpatory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import RegimeTransitionFlag
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "regime-transition-tagger"

PART_DECLARATION = PartDeclaration(
    part_id="regime-transition-tagger",
    consumes=("closed-trade", "market-regime", "regime-break-alert"),
    produces=("regime-transition-flag", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TAGGED = "tagged"
NO_CHANGE = "the-regime-held-for-the-whole-trade"
FLICKER_ONLY = "the-regime-flickered-without-persisting"
NO_REGIME_DATA = "no-regime-was-recorded-over-this-trades-life"


@dataclass(frozen=True)
class TransitionOutcome:
    trade_id: str
    state: str
    flag: RegimeTransitionFlag | None
    observations: int
    reason: str
    tagged_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.flag is not None


@dataclass
class TaggerStanding:
    trades_tagged: int = 0
    transitions_found: int = 0
    flickers_rejected: int = 0
    no_change: int = 0
    without_regime_data: int = 0
    largest_share_in_a_new_regime: float = 0.0


class RegimeTransitionTagger:
    """Tags a trade with whether the market changed character while it was open."""

    def __init__(
        self,
        minimum_persistence_seconds: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_persistence_seconds <= 0:
            raise ValueError(
                "a transition must persist to be a transition; a flicker is the detector "
                "being noisy, and a tagger that finds a change everywhere makes every "
                "inconvenient loss excusable"
            )
        if minimum_observations < 2:
            raise ValueError("a change needs at least two readings to be visible")
        self._minimum_persistence = minimum_persistence_seconds
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        self._regimes: dict[tuple, list] = {}
        self.standing = TaggerStanding()

    def observe_regime(self, venue_id: str, symbol: str, regime: str, at_ns: int) -> None:
        self._regimes.setdefault((venue_id, symbol), []).append((at_ns, regime))

    def readings_over(self, venue_id, symbol, start_ns, end_ns) -> list:
        return sorted(
            (at_ns, regime)
            for at_ns, regime in self._regimes.get((venue_id, symbol), [])
            if start_ns <= at_ns <= end_ns
        )

    def tag(self, trade_id: str, closed_trade) -> TransitionOutcome:
        self.standing.trades_tagged += 1
        readings = self.readings_over(
            closed_trade.venue_id, closed_trade.symbol,
            closed_trade.opened_at_ns, closed_trade.closed_at_ns,
        )

        if len(readings) < self._minimum_observations:
            self.standing.without_regime_data += 1
            return self._outcome(
                trade_id, NO_REGIME_DATA, None, len(readings),
                f"{len(readings)} regime reading(s) over this trade's life, below the "
                f"{self._minimum_observations} needed. Absence of a transition is not "
                f"evidence that the regime held",
            )

        entry_regime = readings[0][1]
        exit_regime = readings[-1][1]

        # Find the last change that persisted to the end of the trade, so a flicker
        # back and forth does not read as a transition.
        transition_at = None
        new_regime = None
        for index in range(len(readings) - 1, 0, -1):
            at_ns, regime = readings[index]
            if regime != readings[index - 1][1]:
                held_for = (closed_trade.closed_at_ns - at_ns) / 1e9
                # The regime must also differ from the one the trade was entered in:
                # a change away and back is a round trip, and the trade ended in the
                # conditions it started in.
                if (
                    held_for >= self._minimum_persistence
                    and regime == exit_regime
                    and regime != entry_regime
                ):
                    transition_at = at_ns
                    new_regime = regime
                break

        changed_at_all = entry_regime != exit_regime

        if not changed_at_all and transition_at is None:
            self.standing.no_change += 1
            return self._outcome(
                trade_id, NO_CHANGE,
                RegimeTransitionFlag(
                    trade_id=trade_id, regime_at_entry=entry_regime,
                    regime_at_exit=exit_regime, changed=False, changed_at_ns=None,
                    fraction_of_the_trade_in_the_new_regime=None,
                    reason=(
                        f"the market stayed {entry_regime} throughout, so this is the "
                        f"trade that was decided on"
                    ),
                    tagged_at_ns=self._now_ns(),
                ),
                len(readings),
                f"regime held at {entry_regime}",
            )

        if transition_at is None:
            self.standing.flickers_rejected += 1
            return self._outcome(
                trade_id, FLICKER_ONLY,
                RegimeTransitionFlag(
                    trade_id=trade_id, regime_at_entry=entry_regime,
                    regime_at_exit=exit_regime, changed=False, changed_at_ns=None,
                    fraction_of_the_trade_in_the_new_regime=None,
                    reason=(
                        f"the regime moved between {entry_regime} and {exit_regime} "
                        f"without holding for {self._minimum_persistence:.0f}s. That is "
                        f"the detector being noisy, not the market changing character"
                    ),
                    tagged_at_ns=self._now_ns(),
                ),
                len(readings),
                "flicker only",
            )

        total = max(closed_trade.closed_at_ns - closed_trade.opened_at_ns, 1)
        share = (closed_trade.closed_at_ns - transition_at) / total
        self.standing.transitions_found += 1
        self.standing.largest_share_in_a_new_regime = max(
            self.standing.largest_share_in_a_new_regime, share
        )

        return self._outcome(
            trade_id, TAGGED,
            RegimeTransitionFlag(
                trade_id=trade_id, regime_at_entry=entry_regime,
                regime_at_exit=new_regime, changed=True, changed_at_ns=transition_at,
                fraction_of_the_trade_in_the_new_regime=share,
                reason=(
                    f"the market changed from {entry_regime} to {new_regime} with "
                    f"{share:.0%} of the trade remaining. That is a fact, not an excuse: "
                    f"the share is what decides how much it explains"
                ),
                tagged_at_ns=self._now_ns(),
            ),
            len(readings),
            f"{entry_regime} to {new_regime}, {share:.0%} of the trade in the new regime",
        )

    def _outcome(self, trade_id, state, flag, observations, reason) -> TransitionOutcome:
        return TransitionOutcome(
            trade_id=trade_id, state=state, flag=flag, observations=observations,
            reason=reason, tagged_at_ns=self._now_ns(),
        )


def describe_regime_tagging(tagger: RegimeTransitionTagger) -> dict:
    return {
        "part_id": PART_ID,
        "trades_tagged": tagger.standing.trades_tagged,
        "transitions_found": tagger.standing.transitions_found,
        "flickers_rejected": tagger.standing.flickers_rejected,
        "trades_with_no_change": tagger.standing.no_change,
        "trades_without_regime_data": tagger.standing.without_regime_data,
        "largest_share_in_a_new_regime": (
            tagger.standing.largest_share_in_a_new_regime
        ),
        "treats_a_flicker_as_a_transition": False,
        "treats_a_transition_as_an_excuse": False,
    }


def run_regime_transition_tagger(
    tagger: RegimeTransitionTagger, control_socket, read_trades, publish_flags,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade in read_trades():
            outcome = tagger.tag(trade_id, closed_trade)
            if outcome.is_usable:
                publish_flags(outcome.flag)

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    regimes = Batch(read=context.bus.reader("market-regime"))
    breaks = Batch(read=context.bus.reader("regime-break-alert"))
    publish_flags = context.bus.publisher_for("regime-transition-flag")
    tagger = RegimeTransitionTagger(
        minimum_persistence_seconds=context.number("regime_tag_minimum_persistence"),
        minimum_observations=int(context.number("regime_tag_minimum_observations")),
    )

    def read_trades():
        breaks.payloads()
        for regime in regimes.payloads():
            tagger.observe_regime(regime.venue_id, regime.symbol, regime.regime, regime.classified_at_ns)
        return tuple((closed_trade_id(trade), trade) for trade in closed.payloads())

    return run_regime_transition_tagger(
        tagger=tagger,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_flags=lambda flag: publish_flags((flag,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
