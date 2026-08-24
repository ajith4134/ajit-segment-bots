"""bear-exit-plan-proposer: where a short is wrong, and why its stop is not the bull's.

The bull's plan and this one are built from the same records and are not
symmetric, because a short's payoff is not symmetric:

- **The stop sits above and its loss is unbounded.** A long that goes to zero
  loses what it put in. A short that doubles loses the same again, and there is
  no price at which it stops getting worse. So the stop is not merely placed past
  the adverse excursion -- it is also **capped in absolute distance**, and a
  symbol whose recorded excursions are wider than that cap gets no plan at all.
  A stop that would only be reached after the position has lost more than the
  account can carry is not a stop.
- **The targets are bounded by arithmetic.** A short's maximum favourable
  excursion is 100%: price cannot fall below zero. Excursion quantiles are
  clamped to that, because a record built from percentage moves in a crash can
  otherwise produce a target below zero, which is not a low target -- it is an
  order the venue will reject.
- **The horizon is shorter.** Funding accrues against the short in exactly the
  regimes where it looks attractive, so the record's median is used as it is
  rather than being extended for conviction the way the bull's is.

**A plan that cannot be built is not built.** No default stop, no fallback
percentage (RL-062).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.bot_opinion import SHORT, ExitPlan, ExitTarget
from runtime.part_declaration import PartDeclaration
# Defined once, in the substrate. They were defined here and again in the
# peer bot's proposer, and the bus pickles -- so a profile produced against
# one definition arrived at the other as a class it did not recognise.
from runtime.trade_profiles import ExcursionProfile, HorizonProfile
from runtime.part_process import run_part

PART_ID = "bear-exit-plan-proposer"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-exit-plan-proposer",
    consumes=(
        "bear-side-candidate", "symbol-price-frame", "symbol-profile",
        "bear-calibrated-conviction", "excursion-profile", "horizon-profile", "stop-audit",
    ),
    produces=("bear-exit-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Price cannot go below zero, so a short's favourable excursion cannot exceed
# this. Not a tuning constant -- arithmetic.
MAXIMUM_SHORT_FAVOURABLE_EXCURSION = 1.0

NO_EXCURSION_PROFILE = "no-excursion-record-for-this-symbol"
NO_PRICE = "no-price-for-this-symbol"
NO_HORIZON = "no-horizon-record-for-this-kind-of-trade"
# The cold-start path measures the stop from this symbol's own recent range;
# too few prints inside the claimed horizon is its own refusal, not a missing
# excursion record.
NO_RANGE = "too-few-prints-in-the-window-to-measure-a-range"
REWARD_BELOW_RISK = "reward-to-risk-below-floor"
STOP_WOULD_BE_UNBOUNDED = "the-stop-this-symbol-needs-is-wider-than-a-short-can-carry"


@dataclass(frozen=True)
class StopAudit:
    """Where short stops have been hit and the trade then went on to work anyway."""

    venue_id: str
    symbol: str
    stops_hit: int
    stops_hit_then_reversed: int
    worst_reversal_excursion: float | None

    @property
    def reversal_fraction(self) -> float | None:
        if not self.stops_hit:
            return None
        return self.stops_hit_then_reversed / self.stops_hit


@dataclass
class ProposerStanding:
    plans_requested: int = 0
    plans_built: int = 0
    stops_widened_by_audit: int = 0
    refused_unbounded: int = 0
    targets_clamped_at_zero: int = 0
    by_refusal: dict = field(default_factory=dict)
    widest_stop_fraction: float = 0.0


class BearExitPlanProposer:
    """Builds a short's stop, targets and horizon, and refuses one it cannot bound."""

    def __init__(
        self,
        stop_safety_multiple: float,
        maximum_stop_fraction: float,
        target_quantiles: tuple,
        minimum_reward_to_risk: float,
        cold_start_stop_range_multiple: float,
        cold_start_reward_multiples: tuple,
        cold_start_minimum_prints: int,
        cold_start_price_window: int,
        now_ns=time.time_ns,
    ) -> None:
        # The cold-start path, ported from the bull proposer on 2026-08-23: before
        # any short has closed there is no excursion record, and a proposer that
        # refused every plan until one existed could never produce the first
        # short that would make one. The stop is measured from this symbol's own
        # range over the claimed horizon, the targets are multiples of that risk.
        if cold_start_stop_range_multiple <= 0:
            raise ValueError("a cold-start stop at or inside the measured range is the range itself")
        if not cold_start_reward_multiples:
            raise ValueError("a cold-start plan with no target never takes profit")
        if len(cold_start_reward_multiples) != len(target_quantiles):
            raise ValueError(
                f"{len(cold_start_reward_multiples)} cold-start reward multiple(s) against "
                f"{len(target_quantiles)} target(s): they are read position by position"
            )
        if cold_start_minimum_prints < 2:
            raise ValueError("a range needs at least two prints")
        if stop_safety_multiple <= 1.0:
            raise ValueError(
                "a stop at or inside the excursion a winner normally survives is a machine "
                "for being stopped out of trades that were going to work"
            )
        if not 0.0 < maximum_stop_fraction < 1.0:
            raise ValueError(
                "a short's loss has no ceiling, so the distance to its stop must have one; "
                "it is a fraction of entry price and must be inside (0, 1)"
            )
        if not target_quantiles:
            raise ValueError("a plan with no target never takes profit")
        if abs(sum(fraction for _, fraction in target_quantiles) - 1.0) > 1e-9:
            raise ValueError(
                "the target fractions must close the whole position, or the plan leaves a "
                "remainder nobody decided to hold"
            )
        self._stop_multiple = stop_safety_multiple
        self._maximum_stop_fraction = maximum_stop_fraction
        self._target_quantiles = tuple(target_quantiles)
        self._minimum_reward_to_risk = minimum_reward_to_risk
        self._cold_start_stop_multiple = cold_start_stop_range_multiple
        self._cold_start_reward_multiples = tuple(cold_start_reward_multiples)
        self._cold_start_minimum_prints = cold_start_minimum_prints
        self._cold_start_price_window = cold_start_price_window
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
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
        input loss a batch spanning a minute arrives at once and reads as a
        minute's range compressed into an instant.
        """
        key = (venue_id, symbol)
        self._prices[key] = price
        window = self._recent.get(key)
        if window is None:
            window = deque(maxlen=self._cold_start_price_window)
            self._recent[key] = window
        window.append((at_ns, price))

    def live_range_fraction(self, venue_id: str, symbol: str, seconds: float):
        """How far this symbol traded through the last `seconds`, as a fraction of price."""
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
        return (highest - lowest) / last, len(inside)

    def observe_symbol_profile(self, venue_id: str, symbol: str, price_step: float) -> None:
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

        horizon = self._horizons.get(candidate.detector)
        claimed_seconds = float(getattr(candidate, "horizon_seconds", 0.0) or 0.0)
        if horizon is not None and horizon.is_fitted:
            horizon_seconds = horizon.median_seconds
            horizon_source = f"{horizon.trades_observed} recorded outcome(s) for this detector"
        elif claimed_seconds > 0:
            horizon_seconds = claimed_seconds
            horizon_source = "the horizon the detector itself claimed"
        else:
            return None, self._refuse(NO_HORIZON)

        profile = self._excursions.get(key)
        measured = profile is not None and profile.is_fitted
        widened = False
        if measured:
            stop_fraction, widened = self._stop_fraction(key, profile)
            stop_source = (
                f"{self._stop_multiple:.2g}x the {profile.adverse_excursion:.2%} adverse "
                f"excursion short winners in this symbol survive"
            )
        else:
            ranged = self.live_range_fraction(candidate.venue_id, candidate.symbol, horizon_seconds)
            if ranged is None:
                return None, self._refuse(NO_RANGE)
            range_fraction, prints = ranged
            stop_fraction = range_fraction * self._cold_start_stop_multiple
            stop_source = (
                f"{self._cold_start_stop_multiple:g}x the {range_fraction:.2%} this symbol "
                f"traded through in {horizon_seconds:.0f}s across {prints} print(s); no short "
                f"has closed in it yet"
            )
        if stop_fraction > self._maximum_stop_fraction:
            # Refused rather than clamped. Clamping would produce a stop inside
            # what this symbol's winners normally survive -- a plan that is
            # bounded and wrong, which is worse than no plan.
            self.standing.refused_unbounded += 1
            return None, self._refuse(STOP_WOULD_BE_UNBOUNDED)

        stop_price = self._on_step(key, price * (1.0 + stop_fraction), round_down=False)
        if stop_price <= price:
            return None, self._refuse(NO_EXCURSION_PROFILE if measured else NO_RANGE)

        targets = (
            self._targets(key, price, profile) if measured
            else self._cold_start_targets(key, price, stop_fraction)
        )
        if not targets:
            return None, self._refuse(NO_EXCURSION_PROFILE if measured else NO_RANGE)

        risk = stop_price - price
        weighted_reward = sum((price - target.price) * target.fraction for target in targets)
        reward_to_risk = weighted_reward / risk if risk > 0 else None

        if reward_to_risk is None or reward_to_risk < self._minimum_reward_to_risk:
            return None, self._refuse(REWARD_BELOW_RISK)

        self.standing.plans_built += 1
        self.standing.widest_stop_fraction = max(self.standing.widest_stop_fraction, stop_fraction)

        return (
            ExitPlan(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=SHORT,
                stop_price=stop_price,
                targets=targets,
                invalidation_reason=(
                    f"a close above {stop_price:.8g} is further against this short than "
                    + (
                        f"{profile.trades_observed} recorded shorts in {candidate.symbol} normally survive"
                        if measured else f"{candidate.symbol} moved through its claimed horizon"
                    )
                    + ", and a short that keeps being wrong keeps getting worse"
                ),
                horizon_seconds=horizon_seconds,
                risk_fraction=stop_fraction,
                reward_to_risk=reward_to_risk,
                reason=(
                    f"stop {stop_fraction:.2%} above {price:.8g}, which is {stop_source}"
                    + (" and widened again by the stop audit" if widened else "")
                    + f", inside the {self._maximum_stop_fraction:.0%} a short is allowed to "
                    f"risk because its loss has no ceiling; {len(targets)} target(s), weighted "
                    f"reward-to-risk {reward_to_risk:.2f}; resolved within "
                    f"{horizon_seconds:.0f}s ({horizon_source}), not extended for conviction "
                    f"because carry accrues against a short every settlement"
                ),
                planned_at_ns=self._now_ns(),
            ),
            "planned",
        )

    def _stop_fraction(self, key, profile: ExcursionProfile) -> tuple[float, bool]:
        fraction = profile.adverse_excursion * self._stop_multiple
        audit = self._audits.get(key)
        widened = False
        if audit is not None and audit.worst_reversal_excursion is not None:
            widened_to = audit.worst_reversal_excursion * self._stop_multiple
            if widened_to > fraction:
                fraction = widened_to
                widened = True
                self.standing.stops_widened_by_audit += 1
        return fraction, widened

    def _cold_start_targets(self, key, price: float, stop_fraction: float) -> tuple:
        """Take-profits below entry at reward-to-risk multiples of the measured stop."""
        targets = []
        for multiple, (_quantile, fraction) in zip(
            self._cold_start_reward_multiples, self._target_quantiles, strict=True
        ):
            reach = min(stop_fraction * multiple, MAXIMUM_SHORT_FAVOURABLE_EXCURSION)
            if reach <= 0:
                continue
            target_price = self._on_step(key, price * (1.0 - reach), round_down=True)
            if target_price <= 0:
                continue
            targets.append(
                ExitTarget(
                    price=target_price,
                    fraction=fraction,
                    reason=(
                        f"{multiple:g}x the {stop_fraction:.2%} this symbol's own range put the "
                        f"stop at; no excursion record exists to place a quantile from yet"
                    ),
                )
            )
        return tuple(targets)

    def _targets(self, key, price: float, profile: ExcursionProfile) -> tuple:
        """Scale-outs below entry, clamped by the fact that price cannot go below zero."""
        targets = []
        for quantile, fraction in self._target_quantiles:
            excursion = profile.favourable_quantiles.get(quantile)
            if excursion is None or excursion <= 0:
                continue
            clamped = min(excursion, MAXIMUM_SHORT_FAVOURABLE_EXCURSION)
            if clamped < excursion:
                self.standing.targets_clamped_at_zero += 1
            target_price = self._on_step(key, price * (1.0 - clamped), round_down=True)
            if target_price <= 0:
                continue
            targets.append(
                ExitTarget(
                    price=target_price,
                    fraction=fraction,
                    reason=(
                        f"the {quantile:.0%} favourable excursion over "
                        f"{profile.trades_observed} recorded shorts is {excursion:.2%}"
                        + (
                            f", clamped to {clamped:.0%} because price cannot fall below zero"
                            if clamped < excursion
                            else ""
                        )
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
                reason=(
                    f"{last.reason}; carries the {1.0 - allocated:.0%} of the position whose "
                    f"target quantile has no recorded excursion, so nothing is left unplanned"
                ),
            )
        return tuple(targets)

    def _on_step(self, key, price: float, round_down: bool) -> float:
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


def describe_exit_planning(proposer: BearExitPlanProposer) -> dict:
    return {
        "part_id": PART_ID,
        "plans_requested": proposer.standing.plans_requested,
        "plans_built": proposer.standing.plans_built,
        "refused_by_reason": dict(proposer.standing.by_refusal),
        "refused_because_the_stop_could_not_be_bounded": proposer.standing.refused_unbounded,
        "targets_clamped_at_zero": proposer.standing.targets_clamped_at_zero,
        "stops_widened_by_audit": proposer.standing.stops_widened_by_audit,
        "widest_stop_fraction": proposer.standing.widest_stop_fraction,
        "symbols_with_an_excursion_profile": len(proposer._excursions),
        "detectors_with_a_horizon_profile": len(proposer._horizons),
    }


def run_bear_exit_plan_proposer(
    proposer: BearExitPlanProposer, control_socket, read_candidates_and_profiles,
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
    )

def _paired_targets(context) -> tuple[tuple[float, float], ...]:
    """Quantiles and the share each takes, zipped from two flat settings; a
    mismatch is refused by name, as in the bull proposer."""
    quantiles = list(context.setting("bear_exit_target_quantiles").value)
    fractions = list(context.setting("bear_exit_target_fractions").value)
    if len(quantiles) != len(fractions):
        raise ValueError(
            f"bear_exit_target_quantiles has {len(quantiles)} entries and "
            f"bear_exit_target_fractions has {len(fractions)}; they are read position by position"
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
    candidates = Batch(read=context.bus.reader("bear-side-candidate"))
    convictions = LatestByKey(
        read=context.bus.reader("bear-calibrated-conviction"),
        key_of=lambda conviction: (conviction.venue_id, conviction.symbol),
    )
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    excursions = Batch(read=context.bus.reader("excursion-profile"))
    horizons = Batch(read=context.bus.reader("horizon-profile"))
    audits = Batch(read=context.bus.reader("stop-audit"))
    publish_plans = context.bus.publisher_for("bear-exit-plan")

    def read_candidates_and_profiles(proposer):
        for trade in levels_in(trades.payloads()):
            proposer.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        for profile in profiles.payloads():
            proposer.observe_symbol_profile(profile)
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

    return run_bear_exit_plan_proposer(
        proposer=BearExitPlanProposer(
            stop_safety_multiple=context.number("bear_exit_stop_safety_multiple"),
            target_quantiles=_paired_targets(context),
            minimum_reward_to_risk=context.number("bear_exit_minimum_reward_to_risk"),
            maximum_stop_fraction=context.number("risk_maximum_stop_fraction"),
            # What the plan is built from before any trade has closed: this
            # symbol's own range over the horizon the detector claimed. Every one
            # of these is replaced the moment the excursion record for that
            # symbol fits, so they set the first trades rather than all of them.
            cold_start_stop_range_multiple=context.number("bear_cold_start_stop_range_multiple"),
            cold_start_reward_multiples=tuple(
                float(multiple)
                for multiple in context.setting("bear_cold_start_reward_multiples").value
            ),
            cold_start_minimum_prints=int(context.number("bear_cold_start_minimum_prints")),
            cold_start_price_window=int(context.number("bear_cold_start_price_window")),
        ),
        control_socket=context.control_socket,
        read_candidates_and_profiles=read_candidates_and_profiles,
        publish_plans=publish_plans,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
