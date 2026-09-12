"""bull-exit-plan-proposer: where this long is wrong, before there is anything to defend.

The plan is built **before entry** on purpose. A stop chosen after a position is
open is chosen by whoever is losing money on it, and the excursion record that
says how far this symbol normally goes against a winner is only usable while
there is no position arguing with it.

Three numbers, each from a measurement rather than a rule of thumb:

- **The stop.** Placed past the excursion a winning trade in this symbol normally
  survives (RL-042). A stop inside that band is not tight risk management, it is
  a machine for being stopped out of trades that were going to work. The
  `stop-audit` record is what corrects this: it says where stops have actually
  been hit and then reversed, and the proposer widens past that.
- **The targets.** Scaled out at the excursion quantiles that have actually been
  reached, not at round multiples of the risk. A 3R target on a symbol that
  reaches 3R twice a year is a plan to never take profit.
- **The horizon.** From the horizon profile: how long this kind of trade has
  taken to resolve. A trade past its horizon is not a trade any more, it is a
  position nobody decided to hold.

**Before any trade has closed, the same three numbers come from live prices.**
Not from a rule of thumb and not from a default percentage -- from this symbol's
own movement, right now:

- **The stop** is a multiple of the range the symbol has actually traded through
  in a window as long as the move the detector is claiming. That is what an ATR
  stop is, and it needs no trade history: a symbol that moves 0.4% in a minute
  gets a wider stop than one that moves 0.05%, automatically and per symbol.
- **The targets** are reward-to-risk multiples of that stop, because before there
  is an excursion record there is no measured quantile to place them at.
- **The horizon** is the one the candidate already names. Every `entry-candidate`
  carries `horizon_seconds` -- the detector saying how long it expects the move to
  take -- and demanding a separately measured horizon before planning anything was
  a gate that never needed to exist.

**This is not the placeholder RL-062 forbids.** A placeholder is a number invented
from nothing and presented as a measurement. Every number above is measured from
the venue's own prints in the last N seconds, the plan says which source produced
it, and each one is replaced the moment the learned profile for that symbol fits.
Learning was meant to improve the trade, not to be a precondition for making one.

What is still refused outright: a symbol with no price at all, and a window with
too few prints in it to state a range. Those produce no plan, and the bot stands
down.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.bot_opinion import LONG, ExitPlan, ExitTarget
from runtime.knowledge_types import TICK_SIZE
from runtime.part_declaration import PartDeclaration
from runtime.range_from_a_small_sample import RangeFromASmallSample
# Defined once, in the substrate. They were defined here and again in the
# peer bot's proposer, and the bus pickles -- so a profile produced against
# one definition arrived at the other as a class it did not recognise.
from runtime.trade_profiles import ExcursionProfile, HorizonProfile
from runtime.trade_decoding_types import StopAudit
from runtime.part_process import run_part

PART_ID = "bull-exit-plan-proposer"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-exit-plan-proposer",
    consumes=(
        "bull-side-candidate", "symbol-price-frame", "symbol-profile",
        "bull-calibrated-conviction", "excursion-profile", "horizon-profile", "stop-audit",
    ),
    produces=("bull-exit-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NO_EXCURSION_PROFILE = "no-excursion-record-for-this-symbol"
NO_PRICE = "no-price-for-this-symbol"
NO_HORIZON = "no-horizon-record-for-this-kind-of-trade"
REWARD_BELOW_RISK = "reward-to-risk-below-floor"
# Not enough prints inside the claimed horizon to state a range. The one refusal
# the cold start keeps: a range over two ticks is those two ticks, and a stop
# placed from it would be a stop placed from noise.
NO_RANGE = "too-few-prints-in-the-window-to-measure-a-range"

# Where a plan's numbers came from. Carried on the plan's reason so a reader can
# tell a stop measured from a symbol's live range from one measured over its
# closed trades -- they are different evidence and the board must not merge them.
FROM_THE_EXCURSION_RECORD = "the-excursion-record"
FROM_THE_LIVE_RANGE = "the-live-price-range"


@dataclass
class ProposerStanding:
    plans_requested: int = 0
    plans_built: int = 0
    stops_widened_by_audit: int = 0
    by_refusal: dict = field(default_factory=dict)
    widest_stop_fraction: float = 0.0
    # Which evidence each plan was built from. Counted separately because a plan
    # from a symbol's live range and one from its closed-trade record are not the
    # same claim, and a board that added them would report a maturity the system
    # has not reached.
    plans_from_the_excursion_record: int = 0
    plans_from_the_live_range: int = 0
    widest_live_range_fraction: float = 0.0
    # How few prints a range was ever measured over, and the largest correction
    # that produced. Rule 8: a stop built from a five-print range corrected 1.66x
    # and one built from fifty prints uncorrected are different claims, and a
    # board that showed only "plans built" could not tell them apart.
    smallest_range_sample: int = 0
    largest_small_sample_correction: float = 0.0


class BullExitPlanProposer:
    """Builds the stop, the targets and the horizon from what has actually happened."""

    def __init__(
        self,
        stop_safety_multiple: float,
        target_quantiles: tuple,
        minimum_reward_to_risk: float,
        conviction_horizon_multiple: float,
        cold_start_stop_range_multiple: float,
        cold_start_reward_multiples: tuple,
        cold_start_minimum_prints: int,
        cold_start_price_window: int,
        range_recovery: RangeFromASmallSample,
        now_ns=time.time_ns,
    ) -> None:
        if stop_safety_multiple <= 1.0:
            raise ValueError(
                "a stop at or inside the excursion a winner normally survives is a machine "
                "for being stopped out of trades that were going to work"
            )
        if not target_quantiles:
            raise ValueError("a plan with no target never takes profit")
        if not all(0.0 < quantile < 1.0 for quantile, _ in target_quantiles):
            raise ValueError("each target names a quantile of the favourable excursion")
        if abs(sum(fraction for _, fraction in target_quantiles) - 1.0) > 1e-9:
            raise ValueError(
                "the target fractions must close the whole position, or the plan leaves "
                "a remainder nobody decided to hold"
            )
        if cold_start_stop_range_multiple <= 0:
            raise ValueError("a stop at the price it was entered at is not a stop")
        if not cold_start_reward_multiples:
            raise ValueError("with no reward multiple there is nowhere to put a cold-start target")
        if len(cold_start_reward_multiples) != len(target_quantiles):
            raise ValueError(
                f"{len(cold_start_reward_multiples)} cold-start reward multiple(s) against "
                f"{len(target_quantiles)} target fraction(s). They are paired position by "
                f"position with the same fractions the measured targets use, so a mismatch is "
                f"a target with no size or a size with no target."
            )
        if cold_start_minimum_prints < 2:
            raise ValueError(
                "a range over one print is that print, reported with the authority of a range"
            )
        self._stop_multiple = stop_safety_multiple
        self._target_quantiles = tuple(target_quantiles)
        self._minimum_reward_to_risk = minimum_reward_to_risk
        self._conviction_horizon_multiple = conviction_horizon_multiple
        self._now_ns = now_ns
        self._cold_start_stop_multiple = cold_start_stop_range_multiple
        self._cold_start_reward_multiples = tuple(cold_start_reward_multiples)
        self._cold_start_minimum_prints = cold_start_minimum_prints
        self._range_recovery = range_recovery
        self._cold_start_price_window = cold_start_price_window
        self._prices: dict[tuple[str, str], float] = {}
        # Recent prints per symbol, with when they arrived, so the range over the
        # window the detector claimed can be measured rather than assumed. Bounded
        # because this is a process's memory and an unbounded price history is a
        # leak with a good excuse (T-3).
        self._recent: dict[tuple[str, str], deque] = {}
        self._price_steps: dict[tuple[str, str], float] = {}
        self._excursions: dict[tuple[str, str], ExcursionProfile] = {}
        self._horizons: dict[str, HorizonProfile] = {}
        self._audits: dict[tuple[str, str], StopAudit] = {}
        self.standing = ProposerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, kept with the venue's own time for it.

        `at_ns` has no default, and it replaces this part's own clock in the recent
        window. The cold-start stop distance is "how far did this symbol trade
        through the last N seconds", and stamping prints with the moment they were
        processed answers that about the reader rather than about the market: under
        input loss a batch of prints spanning a minute arrives at once and reads as
        a minute's range compressed into an instant.
        """
        key = (venue_id, symbol)
        self._prices[key] = price
        window = self._recent.get(key)
        if window is None:
            window = deque(maxlen=self._cold_start_price_window)
            self._recent[key] = window
        window.append((at_ns, price))

    def live_range_fraction(self, venue_id: str, symbol: str, seconds: float):
        """How far this symbol traded through the last `seconds`, as a fraction of price.

        The cold-start stop distance, and the reason it needs no trade history: a
        symbol that swings 0.4% in the claimed horizon gets a wider stop than one
        that swings 0.05%, per symbol and automatically. Returns the fraction and
        how many prints it was measured over, or None when there are too few.

        The window is the horizon the detector itself claimed, so the range is
        measured over exactly the span of the move being predicted rather than
        over some fixed lookback that means a different thing for every detector.
        """
        window = self._recent.get((venue_id, symbol))
        if not window:
            return None
        cutoff = self._now_ns() - int(max(0.0, seconds) * 1e9)
        inside = [price for at_ns, price in window if at_ns >= cutoff]
        if len(inside) < self._cold_start_minimum_prints:
            return None
        highest, lowest, last = max(inside), min(inside), inside[-1]
        if last <= 0 or highest <= lowest:
            return None
        measured = (highest - lowest) / last
        # What the window's whole range probably was, given how few prints
        # measured it. A range over five prints spans about 60% of the range over
        # all of them (runtime/range_from_a_small_sample.py), and using it raw
        # places the stop that much too tight -- the direction that stops a trade
        # out of a move it was right about. None here means the sample is below
        # anything measured, so no claim is made rather than a guessed one.
        corrected = self._range_recovery.corrected(measured, len(inside))
        if corrected is None:
            return None
        self.standing.smallest_range_sample = (
            len(inside) if self.standing.smallest_range_sample == 0
            else min(self.standing.smallest_range_sample, len(inside))
        )
        self.standing.largest_small_sample_correction = max(
            self.standing.largest_small_sample_correction, corrected / measured
        )
        return corrected, len(inside)

    def observe_symbol_profile(self, venue_id: str, symbol: str, price_step: float) -> None:
        """The venue's tick size: a stop that is not on one is not a stop the venue will take."""
        self._price_steps[(venue_id, symbol)] = price_step

    def observe_excursion_profile(self, profile: ExcursionProfile) -> None:
        self._excursions[(profile.venue_id, profile.symbol)] = profile

    def observe_horizon_profile(self, profile: HorizonProfile) -> None:
        self._horizons[profile.detector] = profile

    def observe_stop_audit(self, audit: StopAudit) -> None:
        self._audits[(audit.venue_id, audit.symbol)] = audit

    def propose(self, candidate, conviction) -> tuple[ExitPlan | None, str]:
        self.standing.plans_requested += 1
        key = (candidate.venue_id, candidate.symbol)

        price = self._prices.get(key)
        if price is None or price <= 0:
            return None, self._refuse(NO_PRICE)

        # The horizon the detector itself claimed, unless a measured record for
        # this kind of trade exists and is better. The candidate has always
        # carried it; demanding a separately measured one before planning
        # anything was a gate that never needed to exist.
        horizon = self._horizons.get(candidate.detector)
        claimed_seconds = float(getattr(candidate, "horizon_seconds", 0.0) or 0.0)
        if horizon is not None and horizon.is_fitted:
            base_horizon_seconds = horizon.median_seconds
            horizon_source = f"{horizon.trades_observed} recorded outcome(s) for this detector"
        elif claimed_seconds > 0:
            base_horizon_seconds = claimed_seconds
            horizon_source = "the horizon the detector itself claimed"
        else:
            return None, self._refuse(NO_HORIZON)

        profile = self._excursions.get(key)
        measured = profile is not None and profile.is_fitted

        if measured:
            source = FROM_THE_EXCURSION_RECORD
            stop_fraction, widened = self._stop_fraction(key, profile)
            evidence = (
                f"{profile.trades_observed} recorded outcome(s) in {candidate.symbol} normally "
                f"survive less than this"
            )
        else:
            # No excursion record yet. The stop comes from what this symbol has
            # actually traded through in a window as long as the move being
            # claimed -- measured from the venue's own prints, not defaulted.
            ranged = self.live_range_fraction(
                candidate.venue_id, candidate.symbol, base_horizon_seconds
            )
            if ranged is None:
                return None, self._refuse(NO_RANGE)
            range_fraction, prints = ranged
            source = FROM_THE_LIVE_RANGE
            stop_fraction = range_fraction * self._cold_start_stop_multiple
            widened = False
            self.standing.widest_live_range_fraction = max(
                self.standing.widest_live_range_fraction, range_fraction
            )
            evidence = (
                f"{candidate.symbol} traded through {range_fraction:.2%} over the last "
                f"{base_horizon_seconds:.0f}s across {prints} print(s), and this stop sits "
                f"{self._cold_start_stop_multiple:g}x beyond that"
            )

        stop_price = self._on_step(key, price * (1.0 - stop_fraction), round_down=True)
        if stop_price <= 0 or stop_price >= price:
            return None, self._refuse(NO_EXCURSION_PROFILE if measured else NO_RANGE)

        targets = (
            self._targets(key, price, profile)
            if measured
            else self._cold_start_targets(key, price, stop_fraction)
        )
        if not targets:
            return None, self._refuse(NO_EXCURSION_PROFILE if measured else NO_RANGE)

        risk = price - stop_price
        weighted_reward = sum((target.price - price) * target.fraction for target in targets)
        reward_to_risk = weighted_reward / risk if risk > 0 else None

        if reward_to_risk is None or reward_to_risk < self._minimum_reward_to_risk:
            return None, self._refuse(REWARD_BELOW_RISK)

        # A conviction the bot is surer of is given longer to work, because the
        # base horizon is a median over all such claims and the ones it was sure
        # about are the ones worth waiting on.
        horizon_seconds = base_horizon_seconds * (
            1.0 + self._conviction_horizon_multiple * conviction.probability
        )

        self.standing.plans_built += 1
        if measured:
            self.standing.plans_from_the_excursion_record += 1
        else:
            self.standing.plans_from_the_live_range += 1
        self.standing.widest_stop_fraction = max(self.standing.widest_stop_fraction, stop_fraction)

        return (
            ExitPlan(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=LONG,
                stop_price=stop_price,
                targets=targets,
                invalidation_reason=(
                    f"a close below {stop_price:.8g} is further against this long than "
                    f"{evidence}, so the reason for being long has stopped being true"
                ),
                horizon_seconds=horizon_seconds,
                risk_fraction=stop_fraction,
                reward_to_risk=reward_to_risk,
                reason=(
                    f"stop {stop_fraction:.2%} below {price:.8g}, from {source}: {evidence}"
                    + (" and widened again by the stop audit" if widened else "")
                    + f"; {len(targets)} target(s), weighted reward-to-risk "
                    f"{reward_to_risk:.2f}; resolved within {horizon_seconds:.0f}s on "
                    f"{horizon_source}"
                ),
                planned_at_ns=self._now_ns(),
            ),
            "planned",
        )

    def _stop_fraction(self, key, profile: ExcursionProfile) -> tuple[float, bool]:
        """Past what winners survive, and past what the audit says was too tight."""
        fraction = profile.adverse_excursion * self._stop_multiple
        audit = self._audits.get(key)
        widened = False
        if audit is not None and audit.adverse_excursion_fraction is not None:
            widened_to = audit.adverse_excursion_fraction * self._stop_multiple
            if widened_to > fraction:
                fraction = widened_to
                widened = True
                self.standing.stops_widened_by_audit += 1
        return fraction, widened

    def _cold_start_targets(self, key, price: float, stop_fraction: float) -> tuple:
        """Take-profits at reward-to-risk multiples of the stop this symbol earned.

        Multiples of the risk rather than quantiles of an excursion record,
        because before any trade has closed there is no record to take a quantile
        of. The risk itself is still measured -- it came from this symbol's own
        range -- so a 2R target on a symbol that swings 0.4% and a 2R target on
        one that swings 0.05% are different prices, which is the whole point.

        The same fractions the measured targets use, so the position is closed
        whole either way and switching sources does not change how much is sold
        at each rung.
        """
        targets = []
        for multiple, (_quantile, fraction) in zip(
            self._cold_start_reward_multiples, self._target_quantiles, strict=True
        ):
            reach = stop_fraction * multiple
            if reach <= 0:
                continue
            targets.append(
                ExitTarget(
                    price=self._on_step(key, price * (1.0 + reach), round_down=False),
                    fraction=fraction,
                    reason=(
                        f"{multiple:g}x the {stop_fraction:.2%} this symbol's own range put "
                        f"the stop at; no excursion record exists to place a quantile from yet"
                    ),
                )
            )
        if not targets:
            return ()
        allocated = sum(target.fraction for target in targets)
        if allocated < 1.0:
            last = targets[-1]
            targets[-1] = ExitTarget(
                price=last.price,
                fraction=last.fraction + (1.0 - allocated),
                reason=last.reason + "; carries what the other rungs could not price",
            )
        return tuple(targets)

    def _targets(self, key, price: float, profile: ExcursionProfile) -> tuple:
        """Scale-outs at excursions this symbol has actually reached."""
        targets = []
        for quantile, fraction in self._target_quantiles:
            excursion = profile.favourable_quantiles.get(quantile)
            if excursion is None or excursion <= 0:
                continue
            targets.append(
                ExitTarget(
                    price=self._on_step(key, price * (1.0 + excursion), round_down=False),
                    fraction=fraction,
                    reason=(
                        f"the {quantile:.0%} favourable excursion over "
                        f"{profile.trades_observed} recorded trades is {excursion:.2%}"
                    ),
                )
            )
        if not targets:
            return ()

        # Whatever the profile could not price is folded into the last target,
        # so the plan always closes the whole position rather than leaving a
        # remainder nobody decided to hold.
        allocated = sum(target.fraction for target in targets)
        if allocated < 1.0:
            last = targets[-1]
            targets[-1] = ExitTarget(
                price=last.price,
                fraction=last.fraction + (1.0 - allocated),
                reason=(
                    f"{last.reason}; carries the {1.0 - allocated:.0%} of the position whose "
                    f"target quantile has no recorded excursion, so nothing is left unplanned"
                ),
            )
        return tuple(targets)

    def _on_step(self, key, price: float, round_down: bool) -> float:
        """A price the venue will accept. A stop off the tick grid is not a stop."""
        step = self._price_steps.get(key)
        if step is None or step <= 0:
            return price
        steps = price / step
        whole = int(steps)
        if round_down:
            return whole * step
        return (whole if steps == whole else whole + 1) * step

    def _refuse(self, reason: str) -> str:
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason


def describe_exit_planning(proposer: BullExitPlanProposer) -> dict:
    return {
        "part_id": PART_ID,
        "plans_requested": proposer.standing.plans_requested,
        "plans_built": proposer.standing.plans_built,
        "refused_by_reason": dict(proposer.standing.by_refusal),
        "stops_widened_by_audit": proposer.standing.stops_widened_by_audit,
        "plans_from_the_excursion_record": proposer.standing.plans_from_the_excursion_record,
        "plans_from_the_live_range": proposer.standing.plans_from_the_live_range,
        "widest_live_range_fraction": proposer.standing.widest_live_range_fraction,
        "widest_stop_fraction": proposer.standing.widest_stop_fraction,
        # Rule 8: the smallest sample any stop was measured from, and how much it
        # had to be corrected. A board that showed only "plans built" could not
        # tell a stop from fifty prints from one from five.
        "smallest_range_sample": proposer.standing.smallest_range_sample,
        "largest_small_sample_correction": proposer.standing.largest_small_sample_correction,
        "symbols_with_an_excursion_profile": len(proposer._excursions),
        "detectors_with_a_horizon_profile": len(proposer._horizons),
    }


def run_bull_exit_plan_proposer(
    proposer: BullExitPlanProposer, control_socket, read_candidates_and_profiles,
    publish_plans, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        plans = []
        for candidate, conviction in read_candidates_and_profiles(proposer):
            plan, _ = proposer.propose(candidate, conviction)
            if plan is not None:
                plans.append(plan)
        publish_plans(tuple(plans))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_exit_planning(proposer),
    )


def _paired_targets(context) -> tuple[tuple[float, float], ...]:
    """Quantiles and the share of the position each one takes, as pairs.

    Two flat settings rather than one nested value: a setting whose value needed a
    table would be a schema hiding inside a value, and the settings board could not
    render it or say what changed. Zipped here, with the mismatch refused by name --
    a quantile with no fraction beside it is a target nobody sized.
    """
    quantiles = list(context.setting("bull_exit_target_quantiles").value)
    fractions = list(context.setting("bull_exit_target_fractions").value)
    if len(quantiles) != len(fractions):
        raise ValueError(
            f"bull_exit_target_quantiles has {len(quantiles)} entries and "
            f"bull_exit_target_fractions has {len(fractions)}. They are read position by "
            f"position, so a mismatch is a target with no size or a size with no target."
        )
    return tuple(zip(quantiles, fractions, strict=True))


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The same pairing as the entry timer, for the same reason: a plan for a trade
    nobody has a conviction about is a plan for a trade that will not be taken.

    Three of its inputs -- excursion profiles, horizon profiles and stop audits --
    come from closed-trade decoding and will be empty until trades have closed. The
    proposer already has priors for that case and says which it used, so a stop
    placed before any trade has been decoded is visibly a stop placed on a prior.
    """
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    candidates = Batch(read=context.bus.reader("bull-side-candidate"))
    convictions = LatestByKey(
        read=context.bus.reader("bull-calibrated-conviction"),
        key_of=lambda conviction: (conviction.venue_id, conviction.symbol),
    )
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    excursions = Batch(read=context.bus.reader("excursion-profile"))
    horizons = Batch(read=context.bus.reader("horizon-profile"))
    audits = Batch(read=context.bus.reader("stop-audit"))
    publish_plans = context.bus.publisher_for("bull-exit-plan")

    def read_candidates_and_profiles(proposer):
        for trade in levels_in(trades.payloads()):
            proposer.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        for profile in profiles.payloads():
            # The tick size off the profile's closed key set, not the profile
            # itself: a stop that is not on a tick is not a stop the venue takes,
            # and handing the whole bundle where a float was expected crashed this
            # part the hour symbol-profile-store first ran.
            tick_size = profile.value_of(TICK_SIZE)
            if tick_size is not None and tick_size > 0:
                proposer.observe_symbol_profile(profile.venue_id, profile.symbol, tick_size)
        for excursion in excursions.payloads():
            proposer.observe_excursion_profile(excursion)
        for horizon in horizons.payloads():
            proposer.observe_horizon_profile(horizon)
        for audit in audits.payloads():
            proposer.observe_stop_audit(audit)
        belief = convictions.mapping()
        pairs = []
        for candidate in candidates.payloads():
            conviction = belief.get((candidate.venue_id, candidate.symbol))
            if conviction is not None:
                pairs.append((candidate, conviction))
        return tuple(pairs)

    return run_bull_exit_plan_proposer(
        proposer=BullExitPlanProposer(
            stop_safety_multiple=context.number("bull_exit_stop_safety_multiple"),
            target_quantiles=_paired_targets(context),
            minimum_reward_to_risk=context.number("bull_exit_minimum_reward_to_risk"),
            conviction_horizon_multiple=context.number("bull_exit_conviction_horizon_multiple"),
            # What the plan is built from before any trade has closed: this
            # symbol's own range over the horizon the detector claimed. Every one
            # of these is replaced the moment the excursion record for that
            # symbol fits, so they set the first trades rather than all of them.
            cold_start_stop_range_multiple=context.number("bull_cold_start_stop_range_multiple"),
            cold_start_reward_multiples=tuple(
                float(multiple)
                for multiple in context.setting("bull_cold_start_reward_multiples").value
            ),
            range_recovery=RangeFromASmallSample(
                print_counts=tuple(
                    int(count)
                    for count in context.setting("cold_start_range_recovery_print_counts").value
                ),
                recovered_fractions=tuple(
                    float(fraction)
                    for fraction in context.setting("cold_start_range_recovery_fractions").value
                ),
            ),
            cold_start_minimum_prints=int(context.number("bull_cold_start_minimum_prints")),
            cold_start_price_window=int(context.number("bull_cold_start_price_window")),
        ),
        control_socket=context.control_socket,
        read_candidates_and_profiles=read_candidates_and_profiles,
        publish_plans=publish_plans,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
