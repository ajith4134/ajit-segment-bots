"""loss-inverter: turning what a losing trade taught into a trade that opens in profit.

The user's own idea, and the reason this block exists. A losing trade is the most
expensive information this system buys, and the ordinary use of it -- stop doing
that -- recovers almost none of the cost. If a setup reliably loses, something
reliably profits from it, and the inversion is what turns the tuition into an
edge.

But most losses cannot be inverted, and pretending otherwise is how a system
turns a bad strategy into its mirror image and loses twice:

- **A loss to costs inverts into a loss to costs.** Both sides pay the spread. A
  strategy losing to fees inverted is a strategy losing to fees.
- **A loss to random noise has nothing to invert.** If the setup had no edge in
  either direction, the mirror has none either, and inverting it is how a system
  converts noise into two instructions.
- **A loss to bad execution is not a signal loss.** Fixing the execution recovers
  it; inverting it trades against a setup that was right.
- **A loss to being systematically early or late has a timing fix**, not an
  inversion. Inverting it discards a working setup.

**What can be inverted is a loss to being systematically wrong on direction**, at
a rate the base rate does not explain. That is a real signal pointing the other
way, and it is the only case this part produces a hypothesis from.

**The inverted hypothesis is a hypothesis, not an instruction.** It goes to the
falsifier and the power estimator like anything else -- inverting a loss produces
a claim, and a claim that skipped testing because its origin was a real loss is
still untested.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.learning_types import Hypothesis
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "loss-inverter"

PART_DECLARATION = PartDeclaration(
    part_id="loss-inverter",
    consumes=("decoded-trade-instruction", "loss-cause"),
    produces=("inverted-hypothesis", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# Why a strategy lost. Only one of these inverts.
WRONG_ON_DIRECTION = "systematically-wrong-on-direction"
LOST_TO_COSTS = "costs-exceeded-the-edge"
LOST_TO_NOISE = "no-edge-in-either-direction"
LOST_TO_EXECUTION = "the-setup-was-right-and-the-fills-were-not"
LOST_TO_TIMING = "systematically-early-or-late"
LOST_TO_SIZING = "stopped-out-by-size-rather-than-by-thesis"

INVERTED = "inverted"
NOT_INVERTIBLE = "this-kind-of-loss-does-not-invert"
NOT_SYSTEMATIC_ENOUGH = "the-losses-are-not-systematic-enough-to-be-a-signal"

# What each non-invertible cause needs instead, so the part is useful rather than
# merely refusing.
THE_FIX_FOR = {
    LOST_TO_COSTS: "a cheaper way to express it, or a longer horizon; both sides pay the spread",
    LOST_TO_NOISE: "nothing -- there was no edge in either direction, and inverting noise "
                   "produces two instructions instead of none",
    LOST_TO_EXECUTION: "better execution; the setup was right and inverting it would trade "
                       "against a setup that worked",
    LOST_TO_TIMING: "a timing fix; inverting it discards a working setup",
    LOST_TO_SIZING: "smaller size; the thesis was never tested at the size it was traded",
}


@dataclass(frozen=True)
class LossCause:
    """Why one strategy lost, from whoever decoded the trades."""

    instruction_id: str
    detector: str
    regime: str
    cause: str
    trades: int
    losses: int
    average_loss: float
    evidence: dict


@dataclass
class InverterStanding:
    causes_seen: int = 0
    inverted: int = 0
    not_invertible: int = 0
    not_systematic: int = 0
    by_cause: dict = field(default_factory=dict)
    strongest_inversion: float | None = None


class LossInverter:
    """Turns a systematically wrong direction into a hypothesis, and refuses the rest."""

    def __init__(
        self,
        minimum_trades: int,
        minimum_wrong_rate: float,
        base_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.5 < minimum_wrong_rate < 1.0:
            raise ValueError(
                "a strategy wrong less than half the time is not systematically wrong; below "
                "0.5 this would invert noise"
            )
        self._minimum_trades = minimum_trades
        self._minimum_wrong_rate = minimum_wrong_rate
        self._base_rate = base_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._wrong_rates: dict[str, RateEstimator] = {}
        self._trials: dict[str, int] = {}
        self.standing = InverterStanding()

    def observe_trade(self, instruction_id: str, was_wrong_on_direction: bool) -> None:
        self._wrong_rate_for(instruction_id).observe(was_wrong_on_direction)

    def wrong_rate(self, instruction_id: str) -> Estimate:
        return self._wrong_rate_for(instruction_id).estimate(self._minimum_trades)

    def invert(self, cause: LossCause) -> tuple[Hypothesis | None, str]:
        """One loss cause, turned into a hypothesis or refused with what it needs instead."""
        self.standing.causes_seen += 1
        self.standing.by_cause[cause.cause] = self.standing.by_cause.get(cause.cause, 0) + 1

        if cause.cause != WRONG_ON_DIRECTION:
            self.standing.not_invertible += 1
            return None, NOT_INVERTIBLE

        wrong = self.wrong_rate(cause.instruction_id)
        if not wrong.is_fitted or wrong.value < self._minimum_wrong_rate:
            # Being wrong at the base rate is not information. Inverting it
            # converts noise into an instruction.
            self.standing.not_systematic += 1
            return None, NOT_SYSTEMATIC_ENOUGH

        family = f"inverted:{cause.detector}"
        self._trials[family] = self._trials.get(family, 0) + 1
        excess = wrong.value - self._base_rate
        if (
            self.standing.strongest_inversion is None
            or excess > self.standing.strongest_inversion
        ):
            self.standing.strongest_inversion = excess

        self.standing.inverted += 1
        return (
            Hypothesis(
                hypothesis_id=f"{family}:{self._trials[family]}",
                statement=(
                    f"taking the opposite side of {cause.detector}'s calls in the "
                    f"{cause.regime} regime resolves more often than this system's base rate"
                ),
                what_would_refute_it=(
                    f"the opposite side resolving at or below {self._base_rate:.0%} over a "
                    f"measured sample, which would mean the original was losing to something "
                    f"other than direction"
                ),
                family=family,
                trials_in_family=self._trials[family],
                source=PART_ID,
                context={
                    "detector": cause.detector,
                    "regime": cause.regime,
                    "inverted_from": cause.instruction_id,
                },
                required_sample_size=None,
                regime_tag=cause.regime,
                novelty=None,
                evidence={
                    "wrong_on_direction_rate": wrong.value,
                    "trades": cause.trades,
                    "losses": cause.losses,
                    "average_loss": cause.average_loss,
                    "excess_over_base_rate": excess,
                    **cause.evidence,
                },
                proposed_at_ns=self._now_ns(),
            ),
            INVERTED,
        )

    def what_it_needs_instead(self, cause: str) -> str | None:
        """For a loss that does not invert, what would actually fix it."""
        return THE_FIX_FOR.get(cause)

    def _wrong_rate_for(self, instruction_id: str) -> RateEstimator:
        estimator = self._wrong_rates.get(instruction_id)
        if estimator is None:
            estimator = RateEstimator(
                prior=1.0 - self._base_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._wrong_rates[instruction_id] = estimator
        return estimator


def describe_inversion(inverter: LossInverter) -> dict:
    return {
        "part_id": PART_ID,
        "causes_seen": inverter.standing.causes_seen,
        "inverted": inverter.standing.inverted,
        "not_invertible": inverter.standing.not_invertible,
        "not_systematic_enough": inverter.standing.not_systematic,
        "by_cause": dict(sorted(inverter.standing.by_cause.items())),
        "strongest_inversion": inverter.standing.strongest_inversion,
        "invertible_causes": [WRONG_ON_DIRECTION],
        "produces_instructions": False,
    }


def run_loss_inverter(
    inverter: LossInverter, control_socket, read_loss_causes, publish_hypotheses,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        hypotheses = []
        for cause in read_loss_causes(inverter):
            hypothesis, _ = inverter.invert(cause)
            if hypothesis is not None:
                hypotheses.append(hypothesis)
        publish_hypotheses(tuple(hypotheses))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
