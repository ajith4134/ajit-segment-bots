"""trading-halt-decider: stop, and say what would start it again.

This is the part that has to work when everything else is failing, so it is built to
the opposite standard from the rest of the system: it prefers false positives, it
takes the most restrictive input rather than a consensus, and it does the simplest
thing that could possibly work.

The asymmetry is the whole design. **A halt costs opportunity; a runaway costs
capital.** So every input can halt and no input can un-halt on its own -- clearing
requires the condition to be gone *and* the clearing condition to be satisfied.

- **A human override halts and nothing else clears it.** It is not weighed against
  anything, and no reasoning in this system can conclude that a person did not mean it.
- **A venue unreachable with exposure halts entering** but explicitly permits closing.
  A halt that also blocks exits leaves the system unable to reduce risk, which is the
  opposite of what a halt is for.
- **Missing inputs halt.** If the exposure view has not reported, this part does not
  know what is open, and trading without knowing what is open is the state that ends
  accounts.
- **The scope is stated.** Halting one symbol, one venue, or everything are different
  decisions, and a part that only knows how to stop everything gets ignored.

Every halt names what would clear it. A halt with no stated clearing condition is a
shutdown wearing a temporary label, and the difference matters to whoever is deciding
whether to intervene.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import CRITICAL, SHUTDOWN, TradingHalt
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trading-halt-decider"

PART_DECLARATION = PartDeclaration(
    part_id="trading-halt-decider",
    consumes=(
        "autonomy-envelope", "exposure-view", "outage-state", "regime-break-alert",
        "survival-tier", "human-override", "market-anomaly",
    ),
    produces=("trading-halt", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TRADING = "trading"
HALTED = "halted"

# Every cause that halts. Any one is sufficient.
HUMAN_OVERRIDE = "a-human-said-stop"
ENVELOPE_FORBIDS_TRADING = "the-autonomy-envelope-does-not-permit-trading"
EXPOSURE_UNKNOWN = "this-system-does-not-know-what-is-open"
VENUE_UNREACHABLE_WITH_EXPOSURE = "positions-are-open-somewhere-unreachable"
REGIME_BROKE = "the-market-stopped-behaving-like-the-one-that-was-modelled"
MARKET_ANOMALY = "the-market-data-does-not-make-sense"
NO_RUNWAY = "there-is-not-enough-resource-left-to-manage-a-position"

HALT_CAUSES = (
    HUMAN_OVERRIDE, ENVELOPE_FORBIDS_TRADING, EXPOSURE_UNKNOWN,
    VENUE_UNREACHABLE_WITH_EXPOSURE, REGIME_BROKE, MARKET_ANOMALY, NO_RUNWAY,
)

# What clears each. A halt with no clearing condition is a shutdown.
CLEARED_BY = {
    HUMAN_OVERRIDE: "a person removing the override",
    ENVELOPE_FORBIDS_TRADING: "the autonomy envelope widening again",
    EXPOSURE_UNKNOWN: "the exposure view reporting",
    VENUE_UNREACHABLE_WITH_EXPOSURE: "the venue becoming reachable, or the position closing",
    REGIME_BROKE: "the regime detector reporting a stable regime again",
    MARKET_ANOMALY: "market data that passes its own coherence checks",
    NO_RUNWAY: "the survival tier recovering",
}

# Causes that must still allow closing: a halt that blocks exits cannot reduce risk.
STILL_ALLOW_CLOSING = (
    VENUE_UNREACHABLE_WITH_EXPOSURE, REGIME_BROKE, MARKET_ANOMALY, NO_RUNWAY,
    ENVELOPE_FORBIDS_TRADING, EXPOSURE_UNKNOWN,
)

EVERYTHING = "everything"


@dataclass(frozen=True)
class HaltDecision:
    state: str
    halt: TradingHalt
    causes: tuple
    reason: str
    decided_at_ns: int

    @property
    def blocks_closing(self) -> bool:
        return self.halt.is_halted and not self.halt.may_close_positions


@dataclass
class DeciderStanding:
    decisions: int = 0
    halts: int = 0
    resumptions: int = 0
    by_cause: dict = field(default_factory=dict)
    times_closing_was_permitted_during_a_halt: int = 0
    times_it_halted_on_a_missing_input: int = 0


class TradingHaltDecider:
    """Halts on any one cause, states the scope, and always names what would clear it."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._override_active = False
        self._envelope_permits_trading: bool | None = None
        self._exposure_known: bool | None = None
        self._unreachable_venues: set = set()
        self._regime_broken: set = set()
        self._anomalous: set = set()
        self._tier: str | None = None
        self._was_halted = False
        self.standing = DeciderStanding()

    def observe_override(self, is_active: bool) -> None:
        self._override_active = is_active

    def observe_envelope(self, may_trade: bool) -> None:
        self._envelope_permits_trading = may_trade

    def observe_exposure_view(self, is_known: bool) -> None:
        self._exposure_known = is_known

    def observe_outage(self, venue_id: str, is_dangerous: bool) -> None:
        if is_dangerous:
            self._unreachable_venues.add(venue_id)
        else:
            self._unreachable_venues.discard(venue_id)

    def observe_regime_break(self, symbol: str, is_broken: bool) -> None:
        if is_broken:
            self._regime_broken.add(symbol)
        else:
            self._regime_broken.discard(symbol)

    def observe_anomaly(self, symbol: str, is_anomalous: bool) -> None:
        if is_anomalous:
            self._anomalous.add(symbol)
        else:
            self._anomalous.discard(symbol)

    def observe_survival_tier(self, tier: str) -> None:
        self._tier = tier

    def decide(self) -> HaltDecision:
        self.standing.decisions += 1
        causes: list = []
        scope = EVERYTHING

        if self._override_active:
            causes.append(HUMAN_OVERRIDE)
        if self._envelope_permits_trading is False:
            causes.append(ENVELOPE_FORBIDS_TRADING)
        # A missing input halts: trading without knowing what is open ends accounts.
        if self._exposure_known is not True:
            causes.append(EXPOSURE_UNKNOWN)
            self.standing.times_it_halted_on_a_missing_input += 1
        if self._unreachable_venues:
            causes.append(VENUE_UNREACHABLE_WITH_EXPOSURE)
        if self._regime_broken:
            causes.append(REGIME_BROKE)
        if self._anomalous:
            causes.append(MARKET_ANOMALY)
        if self._tier in (CRITICAL, SHUTDOWN):
            causes.append(NO_RUNWAY)

        if not causes:
            if self._was_halted:
                self.standing.resumptions += 1
            self._was_halted = False
            return self._decision(
                TRADING,
                TradingHalt(
                    is_halted=False, causes=(), scope="none", cleared_by="",
                    may_close_positions=True,
                    reason="no cause to halt; every input reported and none of them stops "
                           "trading",
                    decided_at_ns=self._now_ns(),
                ),
                (),
                "trading",
            )

        # Scope: symbol-level causes narrow it only if they are the only causes.
        symbol_only = set(causes) <= {REGIME_BROKE, MARKET_ANOMALY}
        if symbol_only:
            affected = sorted(self._regime_broken | self._anomalous)
            scope = ",".join(affected)

        # A halt that blocks exits leaves the system unable to reduce risk.
        may_close = all(cause in STILL_ALLOW_CLOSING for cause in causes)
        if may_close:
            self.standing.times_closing_was_permitted_during_a_halt += 1

        for cause in causes:
            self.standing.by_cause[cause] = self.standing.by_cause.get(cause, 0) + 1
        if not self._was_halted:
            self.standing.halts += 1
        self._was_halted = True

        return self._decision(
            HALTED,
            TradingHalt(
                is_halted=True,
                causes=tuple(causes),
                scope=scope,
                cleared_by="; ".join(CLEARED_BY[cause] for cause in causes),
                may_close_positions=may_close,
                reason=(
                    f"halted over {scope} by {len(causes)} cause(s): "
                    f"{', '.join(causes)}. "
                    + (
                        "Closing positions is still permitted: a halt that blocks exits "
                        "cannot reduce risk, which is the opposite of what it is for"
                        if may_close
                        else "Closing is also blocked, because a person said stop and "
                             "nothing here may decide they did not mean it"
                    )
                ),
                decided_at_ns=self._now_ns(),
            ),
            tuple(causes),
            f"halted: {', '.join(causes)}",
        )

    def _decision(self, state, halt, causes, reason) -> HaltDecision:
        return HaltDecision(
            state=state, halt=halt, causes=causes, reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_halting(decider: TradingHaltDecider) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": decider.standing.decisions,
        "halts": decider.standing.halts,
        "resumptions": decider.standing.resumptions,
        "by_cause": dict(decider.standing.by_cause),
        "times_closing_was_permitted_during_a_halt": (
            decider.standing.times_closing_was_permitted_during_a_halt
        ),
        "times_it_halted_on_a_missing_input": (
            decider.standing.times_it_halted_on_a_missing_input
        ),
        "halt_causes": list(HALT_CAUSES),
        "requires_consensus_to_halt": False,
        "issues_a_halt_with_no_clearing_condition": False,
    }


def run_trading_halt_decider(
    decider: TradingHaltDecider, control_socket, read_state, publish_halt,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_state(decider)
        publish_halt(decider.decide().halt)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
