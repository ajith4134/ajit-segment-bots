"""power-estimator: how many trades it takes to know whether a hypothesis is true.

The number every other part in this block depends on and that nobody computes by
default. Without it, "test it and see" means testing until the answer looks good,
which is the definition of the thing this system is trying not to do.

The estimate is the standard two-proportion sample size: to detect a hit rate of
`p` against a base rate of `p0`, at a stated significance and power, you need

    n = (z_alpha * sqrt(2 * p_bar * (1 - p_bar)) + z_beta * sqrt(p0*(1-p0) + p*(1-p)))^2
        / (p - p0)^2

and the shape of that formula is the whole message: **the required sample scales
with the inverse square of the effect**. A hypothesis claiming a 10-point edge
needs a few dozen trades; one claiming 2 points needs hundreds, and most claims
are the second kind.

**The significance is the corrected one.** A hypothesis from a family of two
hundred needs a smaller alpha, which needs a larger sample, and estimating power
at an uncorrected 5% understates every sample size in the system.

**A hypothesis whose required sample exceeds what this system can produce is
reported as untestable**, not as needing a long wait. At two trades a day, forty
thousand trades is fifty years, and a system that queues that hypothesis has
quietly decided never to answer it.

**Estimated in trades, not in days.** Days depend on how often the setup appears,
which is the scanner's business.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "power-estimator"

PART_DECLARATION = PartDeclaration(
    part_id="power-estimator",
    consumes=("expectancy-breakdown", "candidate-formula"),
    produces=("required-sample-size", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

ESTIMATED = "estimated"
NO_EFFECT_CLAIMED = "the-hypothesis-claims-no-effect-to-detect"
UNTESTABLE_HERE = "more-trades-than-this-system-can-produce-in-a-lifetime"

# Normal quantiles for the levels this system uses. Tabulated rather than
# computed because the table is exact and readable, and because an inverse
# normal implemented by hand is a subtle thing to get wrong.
Z_FOR_SIGNIFICANCE = {
    0.100: 1.6449, 0.050: 1.9600, 0.025: 2.2414, 0.010: 2.5758,
    0.005: 2.8070, 0.001: 3.2905, 0.0005: 3.4808, 0.0001: 3.8906,
}
Z_FOR_POWER = {0.50: 0.0000, 0.70: 0.5244, 0.80: 0.8416, 0.90: 1.2816, 0.95: 1.6449}


@dataclass(frozen=True)
class RequiredSampleSize:
    """How many closed trades it would take to know, and whether that is reachable."""

    hypothesis_id: str
    state: str
    trades_required: int | None
    claimed_hit_rate: float
    base_rate: float
    effect_size: float
    significance_used: float
    power_used: float
    trials_in_family: int
    reason: str
    estimated_at_ns: int

    @property
    def is_testable(self) -> bool:
        return self.state == ESTIMATED

    def trades_remaining(self, trades_so_far: int) -> int | None:
        if self.trades_required is None:
            return None
        return max(0, self.trades_required - trades_so_far)


@dataclass
class EstimatorStanding:
    estimates: int = 0
    testable: int = 0
    untestable: int = 0
    no_effect_claimed: int = 0
    largest_sample_required: int | None = None
    smallest_effect_testable: float | None = None


class PowerEstimator:
    """Computes how many trades a claim needs before it can be believed or refused."""

    def __init__(
        self,
        power: float,
        maximum_testable_trades: int,
        now_ns=time.time_ns,
    ) -> None:
        if power not in Z_FOR_POWER:
            raise ValueError(
                f"power must be one of {sorted(Z_FOR_POWER)}; an arbitrary value would need an "
                f"inverse normal implemented by hand, which is a subtle thing to get wrong"
            )
        if maximum_testable_trades < 1:
            raise ValueError(
                "a system that can produce no trades can test nothing, and saying so is the "
                "point of this bound"
            )
        self._power = power
        self._z_power = Z_FOR_POWER[power]
        self._maximum = maximum_testable_trades
        self._now_ns = now_ns
        self.standing = EstimatorStanding()

    def z_for(self, significance: float) -> float:
        """The normal quantile for a significance level, from the nearest tabulated one.

        Rounded toward the stricter level, so an unlisted alpha never produces a
        smaller sample than it should.
        """
        if significance in Z_FOR_SIGNIFICANCE:
            return Z_FOR_SIGNIFICANCE[significance]
        stricter = [level for level in Z_FOR_SIGNIFICANCE if level <= significance]
        chosen = min(stricter) if stricter else min(Z_FOR_SIGNIFICANCE)
        return Z_FOR_SIGNIFICANCE[chosen]

    def trades_for(self, claimed: float, base_rate: float, significance: float) -> int | None:
        """The two-proportion sample size. Scales with the inverse square of the effect."""
        effect = abs(claimed - base_rate)
        if effect <= 0:
            return None
        pooled = (claimed + base_rate) / 2
        z_alpha = self.z_for(significance)
        numerator = (
            z_alpha * math.sqrt(2 * pooled * (1 - pooled))
            + self._z_power
            * math.sqrt(base_rate * (1 - base_rate) + claimed * (1 - claimed))
        ) ** 2
        return int(math.ceil(numerator / effect ** 2))

    def estimate(
        self,
        hypothesis_id: str,
        claimed_hit_rate: float,
        base_rate: float,
        corrected_significance: float,
        trials_in_family: int = 1,
    ) -> RequiredSampleSize:
        """One hypothesis, at the significance its own search implies."""
        self.standing.estimates += 1
        effect = abs(claimed_hit_rate - base_rate)

        if effect <= 0:
            self.standing.no_effect_claimed += 1
            return self._sample(
                hypothesis_id, NO_EFFECT_CLAIMED, None, claimed_hit_rate, base_rate, effect,
                corrected_significance, trials_in_family,
                f"the hypothesis claims {claimed_hit_rate:.1%} against a base rate of "
                f"{base_rate:.1%}: no effect to detect, so no sample size can be computed",
            )

        trades = self.trades_for(claimed_hit_rate, base_rate, corrected_significance)

        if trades is not None and trades > self._maximum:
            # At two trades a day, forty thousand trades is fifty years, and
            # queueing that hypothesis is deciding never to answer it.
            self.standing.untestable += 1
            return self._sample(
                hypothesis_id, UNTESTABLE_HERE, trades, claimed_hit_rate, base_rate, effect,
                corrected_significance, trials_in_family,
                f"detecting a {effect:.1%} effect at {corrected_significance:.5f} significance "
                f"with {self._power:.0%} power needs {trades:,} closed trade(s), past the "
                f"{self._maximum:,} this system can produce. That is not a long wait, it is a "
                f"question this system has quietly decided never to answer",
            )

        self.standing.testable += 1
        if self.standing.largest_sample_required is None or trades > self.standing.largest_sample_required:
            self.standing.largest_sample_required = trades
        if (
            self.standing.smallest_effect_testable is None
            or effect < self.standing.smallest_effect_testable
        ):
            self.standing.smallest_effect_testable = effect

        return self._sample(
            hypothesis_id, ESTIMATED, trades, claimed_hit_rate, base_rate, effect,
            corrected_significance, trials_in_family,
            f"detecting {claimed_hit_rate:.1%} against a {base_rate:.1%} base rate -- a "
            f"{effect:.1%} effect -- at {corrected_significance:.5f} significance with "
            f"{self._power:.0%} power needs {trades:,} closed trade(s)"
            + (
                f". The significance is the corrected one for {trials_in_family} trial(s); "
                f"estimating at an uncorrected level would understate this"
                if trials_in_family > 1
                else ""
            )
            + ". Sample scales with the inverse square of the effect, which is why a 2-point "
            "claim costs so much more than a 10-point one",
        )

    def _sample(
        self, hypothesis_id, state, trades, claimed, base_rate, effect,
        significance, trials, reason,
    ) -> RequiredSampleSize:
        return RequiredSampleSize(
            hypothesis_id=hypothesis_id,
            state=state,
            trades_required=trades,
            claimed_hit_rate=claimed,
            base_rate=base_rate,
            effect_size=effect,
            significance_used=significance,
            power_used=self._power,
            trials_in_family=trials,
            reason=reason,
            estimated_at_ns=self._now_ns(),
        )


def describe_power(estimator: PowerEstimator) -> dict:
    return {
        "part_id": PART_ID,
        "estimates": estimator.standing.estimates,
        "testable": estimator.standing.testable,
        "untestable_here": estimator.standing.untestable,
        "claimed_no_effect": estimator.standing.no_effect_claimed,
        "largest_sample_required": estimator.standing.largest_sample_required,
        "smallest_effect_testable": estimator.standing.smallest_effect_testable,
        "power": estimator._power,
        "maximum_testable_trades": estimator._maximum,
    }


def run_power_estimator(
    estimator: PowerEstimator, control_socket, read_hypotheses, publish_sample_sizes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_sample_sizes(
            tuple(
                estimator.estimate(hypothesis_id, claimed, base_rate, significance, trials)
                for hypothesis_id, claimed, base_rate, significance, trials in read_hypotheses(estimator)
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_power(estimator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A mined formula carries the claim and the trial count that found it; a
    breakdown carries a strategy's measured win rate and how many trades made
    it. Each is sized at the significance its own search implies -- the
    operator's level divided by the trials in the family, rounded toward the
    stricter tabulated quantile -- and a breakdown is sized only once it is a
    measurement, since a prior is not a claim.
    """
    from runtime.input_assembly import Batch

    formulas = Batch(read=context.bus.reader("candidate-formula"))
    breakdowns = Batch(read=context.bus.reader("expectancy-breakdown"))
    publish_sample_sizes = context.bus.publisher_for("required-sample-size")
    estimator = PowerEstimator(
        power=context.number("power_target"),
        maximum_testable_trades=int(context.number("hypothesis_maximum_reachable_trades")),
    )
    significance = context.number("power_significance")
    base_rate = context.number("learning_prior_hit_rate")

    def read_hypotheses(_estimator):
        requests = []
        for formula in formulas.payloads():
            claimed = formula.held_out_hit_rate if formula.held_out_hit_rate is not None else formula.fitted_hit_rate
            trials = max(1, int(formula.trials_in_family))
            requests.append((formula.formula_id, float(claimed), float(formula.base_rate), significance / trials, trials))
        for breakdown in breakdowns.payloads():
            if not breakdown.is_measured or not breakdown.win_rate.is_fitted:
                continue
            requests.append((f"{breakdown.detector}:{breakdown.regime}", float(breakdown.win_rate.value), base_rate, significance, 1))
        return tuple(requests)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_sample_sizes(kept)

    return run_power_estimator(
        estimator=estimator,
        control_socket=context.control_socket,
        read_hypotheses=read_hypotheses,
        publish_sample_sizes=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
