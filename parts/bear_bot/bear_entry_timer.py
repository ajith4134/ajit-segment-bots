"""bear-entry-timer: when to short, and the two ways that differs from when to buy.

The bull's timer waits for a pullback because entering an extended long gives
away the part of the move that has already happened. A short's timing problem is
not the mirror of that, and treating it as one is how short books lose money:

1. **Down is fast, up is slow.** A price falling has already produced the
   liquidity that will make the short hard to enter cleanly, and the move that
   pays it is usually finished before a patient entry fills. So a short that
   waits waits for a **bounce**, not a pullback -- and the level it waits at is
   above the current price, which is the opposite direction from the bull's.
2. **Waiting costs carry.** Every settlement spent waiting is funding either paid
   or received, and in the regimes where shorts look attractive it is usually
   paid. A trigger's expiry is therefore not only about stale reasoning; it is a
   cost that accrues while the intention sits unfilled.

**Every waiting answer carries an expiry**, and this bot's is shorter than the
bull's for the reason above.

**A playbook rule can only narrow, never widen.** A learned rule may withhold a
short and may demand a higher bounce; it may not lower the conviction floor or
pull the trigger down toward the current price.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import ENTER_NOW, SHORT, STAND_DOWN, WAIT_FOR_TRIGGER, EntryTiming
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "bear-entry-timer"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-entry-timer",
    consumes=(
        "bear-side-candidate", "market-data", "bear-calibrated-conviction",
        "playbook-rule", "entry-quality",
    ),
    produces=("bear-entry-timing", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CONVICTION_TOO_LOW = "conviction-below-floor"
NO_PRICE = "no-price-for-this-symbol"
PLAYBOOK_WITHHOLDS = "playbook-rule-withholds-entry"
ALREADY_FALLEN = "the-move-this-short-wanted-has-already-happened"


@dataclass(frozen=True)
class PlaybookRule:
    """A learned rule about when this short setup has been worth entering.

    `bounce_fraction` is how far back up this setup has been worth waiting for.
    """

    detector: str
    bounce_fraction: float | None
    withholds: bool
    reason: str


@dataclass
class TimerStanding:
    decisions: int = 0
    entered_now: int = 0
    waiting: int = 0
    stood_down: int = 0
    triggers_expired: int = 0
    by_refusal: dict = field(default_factory=dict)


class BearEntryTimer:
    """Decides when a short should be entered, waiting for a bounce rather than a dip."""

    def __init__(
        self,
        minimum_conviction: float,
        window_length: int,
        minimum_observations: int,
        trigger_validity_seconds: float,
        minimum_extension_quantile: float,
        entry_quality_window: int,
        prior_extension_floor: float,
        prior_entry_cost_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_conviction < 1.0:
            raise ValueError("a conviction floor outside (0, 1) either takes everything or nothing")
        if trigger_validity_seconds <= 0:
            raise ValueError(
                "a trigger with no life is a short taken later by reasoning that has aged out, "
                "having paid carry the whole way"
            )
        self._minimum_conviction = minimum_conviction
        self._window_length = window_length
        self._minimum = minimum_observations
        self._validity_seconds = trigger_validity_seconds
        self._minimum_extension_quantile = minimum_extension_quantile
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._rules: dict[str, PlaybookRule] = {}
        self._entry_quality = QuantileEstimator(
            window=entry_quality_window, prior=prior_entry_cost_fraction
        )
        self._extension_by_detector: dict[str, QuantileEstimator] = {}
        self._entry_quality_window = entry_quality_window
        self._prior_extension_floor = prior_extension_floor
        self.standing = TimerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._prices[key] = window
        window.observe(price)

    def observe_playbook_rule(self, rule: PlaybookRule) -> None:
        self._rules[rule.detector] = rule

    def observe_entry_quality(self, detector: str, extension_at_entry: float, given_away: float) -> None:
        """What entering this far above the mean actually cost, per detector."""
        self._entry_quality.observe(given_away)
        self._extension_for(detector).observe(extension_at_entry)

    def decide(self, candidate, conviction) -> EntryTiming:
        self.standing.decisions += 1

        if conviction.probability < self._minimum_conviction:
            return self._stand_down(
                candidate, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%}, below the "
                f"{self._minimum_conviction:.1%} this bot shorts on "
                f"({'measured' if conviction.is_measured else 'and not yet a measured frequency'})",
            )

        window = self._prices.get((candidate.venue_id, candidate.symbol))
        price = None if window is None else window.latest
        if price is None:
            return self._stand_down(
                candidate, NO_PRICE,
                "no price has arrived for this symbol, so there is no moment to judge",
            )

        rule = self._rules.get(candidate.detector)
        if rule is not None and rule.withholds:
            return self._stand_down(
                candidate, PLAYBOOK_WITHHOLDS,
                f"the playbook rule for {candidate.detector} withholds this short: {rule.reason}",
            )

        mean = window.mean(self._minimum)
        extension = None if mean is None or mean == 0 else (price - mean) / mean

        # How far above its mean this detector's shorts have normally been entered
        # and still worked. Below that the move has already happened and the short
        # is selling into the liquidity that the fall consumed.
        floor = self._extension_for(candidate.detector).estimate(
            self._minimum_extension_quantile, minimum_observations=self._minimum
        )

        if extension is not None and extension < floor.value:
            bounce = rule.bounce_fraction if rule is not None else None
            trigger = self._trigger_price(mean, price, floor.value, bounce)
            self.standing.waiting += 1
            return EntryTiming(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=SHORT,
                action=WAIT_FOR_TRIGGER,
                trigger_price=trigger,
                valid_until_ns=self._now_ns() + int(self._validity_seconds * 1e9),
                quality=self._median_entry_cost(),
                reason=(
                    f"price is {extension:.2%} from its {self._window_length}-observation mean, "
                    f"below the {floor.value:.2%} at which {candidate.detector} shorts have "
                    f"{'normally still worked' if floor.is_fitted else 'not yet been measured, so the setting stands'}; "
                    f"the fall this short wanted has largely happened, so it waits for a bounce "
                    f"to {trigger:.6g} for {self._validity_seconds:g}s -- shorter than a long "
                    f"would wait, because every settlement spent waiting is carry"
                ),
                decided_at_ns=self._now_ns(),
            )

        self.standing.entered_now += 1
        return EntryTiming(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            side=SHORT,
            action=ENTER_NOW,
            trigger_price=price,
            valid_until_ns=self._now_ns() + int(self._validity_seconds * 1e9),
            quality=self._median_entry_cost(),
            reason=(
                f"conviction {conviction.probability:.1%} and price "
                + (
                    f"{extension:.2%} from its mean, "
                    if extension is not None
                    else "with no established mean yet, "
                )
                + f"at or above the {floor.value:.2%} {candidate.detector} shorts work from "
                + ("by measurement" if floor.is_fitted else "by setting, not yet measured")
                + "; down is fast, so a short that waits here usually watches the move finish"
            ),
            decided_at_ns=self._now_ns(),
        )

    def has_expired(self, timing: EntryTiming) -> bool:
        if timing.valid_until_ns is None:
            return False
        expired = self._now_ns() > timing.valid_until_ns
        if expired:
            self.standing.triggers_expired += 1
        return expired

    def _trigger_price(self, mean, price, extension_floor, bounce_fraction) -> float:
        """The bounce this bot would rather short into, never below where price is.

        A playbook rule may demand a higher bounce; it may not pull the trigger
        down toward the current price, because a learned rule must not be able to
        talk the bot into the entry its own record refuses.
        """
        floor_price = mean * (1.0 + extension_floor)
        if bounce_fraction is not None:
            floor_price = max(floor_price, price * (1.0 + abs(bounce_fraction)))
        return max(floor_price, price)

    def _median_entry_cost(self) -> float:
        return self._entry_quality.estimate(0.5, minimum_observations=self._minimum).value

    def _extension_for(self, detector: str) -> QuantileEstimator:
        estimator = self._extension_by_detector.get(detector)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._entry_quality_window, prior=self._prior_extension_floor
            )
            self._extension_by_detector[detector] = estimator
        return estimator

    def _stand_down(self, candidate, refusal: str, reason: str) -> EntryTiming:
        self.standing.stood_down += 1
        self.standing.by_refusal[refusal] = self.standing.by_refusal.get(refusal, 0) + 1
        return EntryTiming(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            side=SHORT,
            action=STAND_DOWN,
            trigger_price=None,
            valid_until_ns=None,
            quality=None,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_timing(timer: BearEntryTimer) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": timer.standing.decisions,
        "entered_now": timer.standing.entered_now,
        "waiting_for_a_bounce": timer.standing.waiting,
        "stood_down": timer.standing.stood_down,
        "triggers_expired": timer.standing.triggers_expired,
        "stood_down_by_reason": dict(timer.standing.by_refusal),
        "playbook_rules_held": len(timer._rules),
        "detectors_with_an_entry_quality_record": len(timer._extension_by_detector),
    }


def run_bear_entry_timer(
    timer: BearEntryTimer, control_socket, read_candidates_and_convictions,
    publish_timings, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        pairs = read_candidates_and_convictions(timer)
        publish_timings(tuple(timer.decide(candidate, conviction) for candidate, conviction in pairs))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
