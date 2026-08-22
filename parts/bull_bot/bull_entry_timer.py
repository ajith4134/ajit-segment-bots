"""bull-entry-timer: when to act on a long, which is not whether to.

A right setup entered at the wrong moment is a losing trade. That is why timing
is its own part rather than a field the conviction model fills in: the question
is answered from different evidence -- the immediate tape, the playbook rule for
this setup, and the entry-quality record of how much was given away by entering
at each moment -- and it can be turned off without turning off the bot's opinion.

Three answers, and standing down is one of them:

- **Enter now.** The move is happening and waiting costs more than it saves.
- **Wait for a trigger.** The setup is real but the price is not; a level is
  named and the timing expires if it is not reached, so a stale intention cannot
  fire hours later into a market that has changed.
- **Stand down.** Not yet, and not with a level either.

**Every waiting answer carries an expiry.** A trigger with no expiry is the
classic way an automated system takes a trade nobody would take: the reasoning
that produced it aged out, but the order did not.

**A playbook rule can only narrow, never widen.** The rules are learned, and the
one thing a learned rule must not be able to do is talk the bot into a trade its
own evidence refuses -- so a rule can withhold entry and can raise the trigger,
and cannot lower the conviction floor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import ENTER_NOW, LONG, STAND_DOWN, WAIT_FOR_TRIGGER, EntryTiming
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "bull-entry-timer"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-entry-timer",
    consumes=(
        "bull-side-candidate", "market-data", "bull-calibrated-conviction",
        "playbook-rule", "entry-quality",
    ),
    produces=("bull-entry-timing", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CONVICTION_TOO_LOW = "conviction-below-floor"
NO_PRICE = "no-price-for-this-symbol"
PLAYBOOK_WITHHOLDS = "playbook-rule-withholds-entry"
CHASING = "price-already-extended-beyond-what-waiting-recovers"


@dataclass(frozen=True)
class PlaybookRule:
    """A learned rule about when this setup has been worth entering.

    `pullback_fraction` is how far back toward the mean this setup has been worth
    waiting for. `withholds` lets a rule that has been consistently wrong stop
    the bot entering on it at all.
    """

    detector: str
    pullback_fraction: float | None
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


class BullEntryTimer:
    """Decides when a long should be entered, and refuses to leave that open-ended."""

    def __init__(
        self,
        minimum_conviction: float,
        window_length: int,
        minimum_observations: int,
        trigger_validity_seconds: float,
        maximum_extension_quantile: float,
        entry_quality_window: int,
        prior_extension_cap: float,
        prior_entry_cost_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_conviction < 1.0:
            raise ValueError("a conviction floor outside (0, 1) either takes everything or nothing")
        if trigger_validity_seconds <= 0:
            raise ValueError(
                "a trigger with no life is a trade taken later by reasoning that has aged out"
            )
        self._minimum_conviction = minimum_conviction
        self._window_length = window_length
        self._minimum = minimum_observations
        self._validity_seconds = trigger_validity_seconds
        self._maximum_extension_quantile = maximum_extension_quantile
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._rules: dict[str, PlaybookRule] = {}
        self._entry_quality = QuantileEstimator(
            window=entry_quality_window, prior=prior_entry_cost_fraction
        )
        self._extension_by_detector: dict[str, QuantileEstimator] = {}
        self._entry_quality_window = entry_quality_window
        self._prior_extension_cap = prior_extension_cap
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
        """What entering at this much extension actually cost, in fraction of price.

        Learned per detector: a burst detector fires late by construction and a
        reversion detector fires early, so one number over both would time
        neither.
        """
        self._entry_quality.observe(given_away)
        self._extension_for(detector).observe(extension_at_entry)

    def decide(self, candidate, conviction) -> EntryTiming:
        self.standing.decisions += 1

        if conviction.probability < self._minimum_conviction:
            return self._stand_down(
                candidate, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%}, below the "
                f"{self._minimum_conviction:.1%} this bot acts on "
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
                f"the playbook rule for {candidate.detector} withholds entry: {rule.reason}",
            )

        mean = window.mean(self._minimum)
        extension = None if mean is None or mean == 0 else (price - mean) / mean

        # How extended this detector's entries normally are when they work. A
        # long taken far past that is chasing, and the cost of chasing is what
        # entry-quality measures.
        cap = self._extension_for(candidate.detector).estimate(
            self._maximum_extension_quantile, minimum_observations=self._minimum
        )
        extension_cap = cap.value

        if extension is not None and extension > extension_cap:
            pullback = rule.pullback_fraction if rule is not None else None
            trigger = self._trigger_price(mean, price, extension_cap, pullback)
            self.standing.waiting += 1
            return EntryTiming(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=LONG,
                action=WAIT_FOR_TRIGGER,
                trigger_price=trigger,
                valid_until_ns=self._now_ns() + int(self._validity_seconds * 1e9),
                quality=self._median_entry_cost(),
                reason=(
                    f"price is {extension:.2%} above its {self._window_length}-observation mean, "
                    f"past the {extension_cap:.2%} at which {candidate.detector} entries have "
                    f"{'normally still worked' if cap.is_fitted else 'not yet been measured, so the setting stands'}; "
                    f"waiting for {trigger:.6g} for "
                    f"{self._validity_seconds:g}s, after which this intention expires rather "
                    f"than firing into a market that has moved on"
                ),
                decided_at_ns=self._now_ns(),
            )

        self.standing.entered_now += 1
        return EntryTiming(
            bot=BOT,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            side=LONG,
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
                + f"inside the {extension_cap:.2%} {candidate.detector} entries work at "
                + ("by measurement" if cap.is_fitted else "by setting, not yet measured")
            ),
            decided_at_ns=self._now_ns(),
        )

    def has_expired(self, timing: EntryTiming) -> bool:
        """Whether a waiting intention has outlived the reasoning that made it."""
        if timing.valid_until_ns is None:
            return False
        expired = self._now_ns() > timing.valid_until_ns
        if expired:
            self.standing.triggers_expired += 1
        return expired

    def _trigger_price(self, mean, price, extension_cap, pullback_fraction) -> float:
        """The price this bot would rather enter at, never above where it already is.

        A playbook rule may ask for a deeper pullback than the extension cap
        implies; it may not ask for a shallower one, because a learned rule must
        not be able to talk the bot into the entry its own record refuses.
        """
        cap_price = mean * (1.0 + extension_cap)
        if pullback_fraction is not None:
            cap_price = min(cap_price, price * (1.0 - abs(pullback_fraction)))
        return min(cap_price, price)

    def _median_entry_cost(self) -> float:
        """What entering has typically cost, in fraction of price."""
        return self._entry_quality.estimate(0.5, minimum_observations=self._minimum).value

    def _extension_for(self, detector: str) -> QuantileEstimator:
        estimator = self._extension_by_detector.get(detector)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._entry_quality_window, prior=self._prior_extension_cap
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
            side=LONG,
            action=STAND_DOWN,
            trigger_price=None,
            valid_until_ns=None,
            quality=None,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_timing(timer: BullEntryTimer) -> dict:
    return {
        "part_id": PART_ID,
        "decisions": timer.standing.decisions,
        "entered_now": timer.standing.entered_now,
        "waiting_for_a_trigger": timer.standing.waiting,
        "stood_down": timer.standing.stood_down,
        "triggers_expired": timer.standing.triggers_expired,
        "stood_down_by_reason": dict(timer.standing.by_refusal),
        "playbook_rules_held": len(timer._rules),
        "detectors_with_an_entry_quality_record": len(timer._extension_by_detector),
    }


def run_bull_entry_timer(
    timer: BullEntryTimer, control_socket, read_candidates_and_convictions,
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
