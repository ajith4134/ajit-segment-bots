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
from collections import deque
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.bot_opinion import ENTER_NOW, LONG, STAND_DOWN, WAIT_FOR_TRIGGER, EntryTiming
from runtime.edge_arithmetic import ConvictionFloor
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "bull-entry-timer"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-entry-timer",
    consumes=(
        "bull-side-candidate", "symbol-price-frame", "bull-calibrated-conviction",
        "playbook-rule", "trade-episode",
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
        conviction_floor: ConvictionFloor,
        window_length: int,
        minimum_observations: int,
        trigger_validity_seconds: float,
        maximum_extension_quantile: float,
        entry_quality_window: int,
        prior_extension_cap: float,
        prior_entry_cost_fraction: float,
        pending_entries_per_detector: int,
        pending_entry_maximum_age_seconds: float,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        gap_warmup_gaps: int | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if trigger_validity_seconds <= 0:
            raise ValueError(
                "a trigger with no life is a trade taken later by reasoning that has aged out"
            )
        if pending_entries_per_detector < 1:
            raise ValueError("a queue of zero holds no pending entry to ever match")
        if pending_entry_maximum_age_seconds <= 0:
            raise ValueError(
                "an entry that can wait forever for a matching episode leaks memory "
                "for every decision whose trade was never taken"
            )
        # The timer decides the moment while the exit plan is still being built,
        # so it applies the lowest floor any plan could later demand -- fee-free
        # break-even at the least reward-to-risk a plan is accepted with. The
        # composer applies the plan's own floor once it exists.
        self._floor, self._floor_reason = conviction_floor.before_any_plan()
        self._window_length = window_length
        self._minimum = minimum_observations
        self._validity_seconds = trigger_validity_seconds
        self._maximum_extension_quantile = maximum_extension_quantile
        self._now_ns = now_ns
        # How long this symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._gap_patience_multiple = gap_patience_multiple
        self._gap_warmup_gaps = gap_warmup_gaps
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._rules: dict[str, PlaybookRule] = {}
        self._entry_quality = QuantileEstimator(
            window=entry_quality_window, prior=prior_entry_cost_fraction
        )
        self._extension_by_detector: dict[str, QuantileEstimator] = {}
        self._entry_quality_window = entry_quality_window
        self._prior_extension_cap = prior_extension_cap
        self._pending_entries_per_detector = pending_entries_per_detector
        self._pending_entry_maximum_age_seconds = pending_entry_maximum_age_seconds
        self._pending: dict[tuple[str, str, str], deque] = {}
        self.standing = TimerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print into this symbol's window, with the venue's own time for it.

        `at_ns` has no default. The window is what "how extended is this move"
        is measured from, and a window computed across a hole in the feed reads
        the reconnect as the extension -- which times an entry against a move
        that never happened.
        """
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
                gap_warmup_gaps=self._gap_warmup_gaps,
            )
            self._prices[key] = window
        window.observe(price, at_ns)

    def observe_playbook_rule(self, rule) -> None:
        """Hold a rule keyed by the detector it narrows entry for.

        `playbook-rule` also carries `runtime.knowledge_types.PlaybookRule`
        (procedural-playbook's own, regime/instruction-keyed shape, not
        detector-keyed) -- two producers on one wire, the same trap
        `stop-adjustment` already is elsewhere in this codebase. That shape
        has no `detector` at all, and reading it as this part's own crashed
        both entry timers on every tick a real one arrived (found live
        2026-08-30). Read defensively and skip what this table cannot key: a
        rule this part cannot place under a detector narrows nothing for it,
        which is the conservative direction to fail in.
        """
        detector = getattr(rule, "detector", None)
        if detector is None:
            return
        self._rules[detector] = rule

    def observe_entry_quality(self, detector: str, extension_at_entry: float, given_away: float) -> None:
        """What entering at this much extension actually cost, in fraction of price.

        Learned per detector: a burst detector fires late by construction and a
        reversion detector fires early, so one number over both would time
        neither.
        """
        self._entry_quality.observe(given_away)
        self._extension_for(detector).observe(extension_at_entry)

    def _remember_pending_entry(
        self, venue_id: str, symbol: str, detector: str, extension_at_entry: float,
        decided_at_ns: int,
    ) -> None:
        """One ENTER_NOW decision, held until a closing trade-episode claims it.

        Bounded on both ends (T-3): a decision whose trade was never taken, or
        never closes, must not wait here forever. Keyed on detector as well as
        symbol so two detectors active on the same symbol never share a queue.
        """
        key = (venue_id, symbol, detector)
        queue = self._pending.setdefault(key, deque(maxlen=self._pending_entries_per_detector))
        queue.append((extension_at_entry, decided_at_ns))

    def match_trade_episode(self, episode) -> None:
        """A closed trade claiming its entry decision, if one is still pending.

        `trade-episode` is not side-specific -- a bull timer must ignore a
        short episode on the same symbol and detector, or it learns from the
        peer bot's trades.
        """
        if episode.action != LONG:
            return
        key = (episode.venue_id, episode.symbol, episode.detector)
        queue = self._pending.get(key)
        if not queue:
            return
        now_ns = self._now_ns()
        while queue and (now_ns - queue[0][1]) / 1e9 > self._pending_entry_maximum_age_seconds:
            queue.popleft()
        if not queue:
            return
        extension_at_entry, _ = queue.popleft()
        conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
        entry_percentile = conditions.get("entry_percentile")
        if entry_percentile is None:
            return
        given_away = max(0.0, 1.0 - entry_percentile)
        self.observe_entry_quality(episode.detector, extension_at_entry, given_away)

    def decide(self, candidate, conviction) -> EntryTiming:
        self.standing.decisions += 1

        if conviction.probability < self._floor:
            return self._stand_down(
                candidate, CONVICTION_TOO_LOW,
                f"conviction is {conviction.probability:.1%}, below the "
                f"{self._floor:.1%} this bot acts on ({self._floor_reason}; "
                f"{'measured' if conviction.is_measured else 'not yet a measured frequency'})",
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
        if extension is not None:
            self._remember_pending_entry(
                candidate.venue_id, candidate.symbol, candidate.detector, extension,
                self._now_ns(),
            )
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
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_timing(timer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The timer decides on a pair: the candidate that was accepted, and the
    conviction the model reached about it. Conviction is a level per symbol -- the
    model's current belief -- and the candidate is the event that asks for a
    decision, so a candidate with no conviction yet is not timed at all rather than
    timed against a belief nobody formed.
    """
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    candidates = Batch(read=context.bus.reader("bull-side-candidate"))
    convictions = LatestByKey(
        read=context.bus.reader("bull-calibrated-conviction"),
        key_of=lambda conviction: (conviction.venue_id, conviction.symbol),
    )
    rules = Batch(read=context.bus.reader("playbook-rule"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    publish_timings = context.bus.publisher_for("bull-entry-timing")

    def read_candidates_and_convictions(timer):
        for trade in levels_in(trades.payloads()):
            timer.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        for rule in rules.payloads():
            timer.observe_playbook_rule(rule)
        for episode in episodes.payloads():
            timer.match_trade_episode(episode)
        belief = convictions.mapping()
        pairs = []
        for candidate in candidates.payloads():
            conviction = belief.get((candidate.venue_id, candidate.symbol))
            if conviction is not None:
                pairs.append((candidate, conviction))
        return tuple(pairs)

    return run_bull_entry_timer(
        timer=BullEntryTimer(
            conviction_floor=ConvictionFloor(
                # What one crossing costs here, not Bybit's perpetual taker rate.
                # round_trip_cost_in_risk_units doubles this, and every caller
                # passed taker_fee_rate until 2026-09-07 -- eight times light,
                # so the break-even probability every bot judged against was
                # computed from a cost that is not this market's.
                fee_rate=context.number("per_side_trading_cost_fraction"),
                margin=context.number("bull_conviction_margin_over_break_even"),
                fallback_reward_to_risk=context.number("bull_exit_minimum_reward_to_risk"),
            ),
            window_length=int(context.number("bull_entry_window_length")),
            minimum_observations=int(context.number("bull_entry_minimum_observations")),
            trigger_validity_seconds=context.number("bull_entry_trigger_validity"),
            maximum_extension_quantile=context.number("bull_entry_maximum_extension_quantile"),
            entry_quality_window=int(context.number("bull_entry_quality_window")),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
            gap_warmup_gaps=int(context.number("price_gap_warmup_gaps")),
            prior_extension_cap=context.number("bull_entry_prior_extension_cap"),
            prior_entry_cost_fraction=context.number("bull_entry_prior_entry_cost_fraction"),
            pending_entries_per_detector=int(context.number("bull_pending_entries_per_detector")),
            pending_entry_maximum_age_seconds=context.number("bull_pending_entry_maximum_age_seconds"),
        ),
        control_socket=context.control_socket,
        read_candidates_and_convictions=read_candidates_and_convictions,
        publish_timings=publish_timings,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
