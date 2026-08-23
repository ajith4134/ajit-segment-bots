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
from dataclasses import dataclass, field

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
        "bear-side-candidate", "market-data", "symbol-profile",
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
        now_ns=time.time_ns,
    ) -> None:
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
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._price_steps: dict[tuple[str, str], float] = {}
        self._excursions: dict[tuple[str, str], ExcursionProfile] = {}
        self._horizons: dict[str, HorizonProfile] = {}
        self._audits: dict[tuple[str, str], StopAudit] = {}
        self.standing = ProposerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        self._prices[(venue_id, symbol)] = price

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

        profile = self._excursions.get(key)
        if profile is None or not profile.is_fitted:
            return None, self._refuse(NO_EXCURSION_PROFILE)

        horizon = self._horizons.get(candidate.detector)
        if horizon is None or not horizon.is_fitted:
            return None, self._refuse(NO_HORIZON)

        stop_fraction, widened = self._stop_fraction(key, profile)
        if stop_fraction > self._maximum_stop_fraction:
            # Refused rather than clamped. Clamping would produce a stop inside
            # what this symbol's winners normally survive -- a plan that is
            # bounded and wrong, which is worse than no plan.
            self.standing.refused_unbounded += 1
            return None, self._refuse(STOP_WOULD_BE_UNBOUNDED)

        stop_price = self._on_step(key, price * (1.0 + stop_fraction), round_down=False)
        if stop_price <= price:
            return None, self._refuse(NO_EXCURSION_PROFILE)

        targets = self._targets(key, price, profile)
        if not targets:
            return None, self._refuse(NO_EXCURSION_PROFILE)

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
                    f"{profile.trades_observed} recorded shorts in {candidate.symbol} normally "
                    f"survive, and a short that keeps being wrong keeps getting worse"
                ),
                horizon_seconds=horizon.median_seconds,
                risk_fraction=stop_fraction,
                reward_to_risk=reward_to_risk,
                reason=(
                    f"stop {stop_fraction:.2%} above {price:.8g}, which is "
                    f"{self._stop_multiple:.2g}x the {profile.adverse_excursion:.2%} adverse "
                    f"excursion short winners in this symbol survive"
                    + (" and widened again by the stop audit" if widened else "")
                    + f", inside the {self._maximum_stop_fraction:.0%} a short is allowed to "
                    f"risk because its loss has no ceiling; {len(targets)} target(s), weighted "
                    f"reward-to-risk {reward_to_risk:.2f}; resolved within "
                    f"{horizon.median_seconds:.0f}s, the recorded median for "
                    f"{candidate.detector}, not extended for conviction because carry accrues "
                    f"against a short every settlement"
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
    )
