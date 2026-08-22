"""skill-tester: whether a skill's rules would actually have helped here.

A skill is a claim, and an untested claim from a book about a different market in
a different decade is worse than no skill -- because it is specific, confident and
wrong in a way that is hard to argue with.

So every skill's decision rules are replayed against episodes this system
actually traded, and the test is deliberately hostile:

- **Against real closed trades**, never invented scenarios (RL-063). A scenario
  constructed to test a rule tests whether the constructor understood the rule.
- **The rule must have fired.** A rule that never triggers on any episode this
  system has seen is not validated by the absence of counterexamples -- it is
  untested, and the two look identical in a naive score.
- **Scored against what actually happened**, including the trades the rule would
  have prevented. A skill whose only measured effect is stopping trades that
  lost is a good skill, and a test that only scores the trades it enabled would
  miss that entirely.
- **Out of sample.** A rule tested on the episodes it was distilled beside is a
  rule tested on its own training data, and the source may well have been
  written after looking at exactly those.

**A skill that helps in one regime and hurts in another is reported that way**,
not averaged. The average describes neither, and the regime split is the whole
usable finding.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-tester"

PART_DECLARATION = PartDeclaration(
    part_id="skill-tester",
    consumes=("skill", "recalled-episode", "trade-episode"),
    produces=("skill-backtest", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TESTED = "tested"
NEVER_FIRED = "no-episode-this-system-has-seen-would-have-triggered-it"
TOO_FEW_EPISODES = "too-few-episodes-to-test-against"
NO_OUT_OF_SAMPLE = "every-episode-predates-the-skill-so-none-is-out-of-sample"

HELPED = "it-would-have-helped"
HURT = "it-would-have-hurt"
REGIME_DEPENDENT = "it-helps-in-some-regimes-and-hurts-in-others"


@dataclass(frozen=True)
class SkillBacktest:
    """How a skill's rules would have done against what actually happened."""

    skill_id: str
    state: str
    verdict: str | None
    score: float | None
    episodes_tested: int
    times_it_fired: int
    trades_it_would_have_prevented: int
    prevented_losses: int
    prevented_wins: int
    by_regime: dict
    out_of_sample_episodes: int
    reason: str
    tested_at_ns: int

    @property
    def is_tested(self) -> bool:
        return self.state == TESTED

    @property
    def helps(self) -> bool:
        return self.verdict == HELPED


@dataclass
class TesterStanding:
    skills_tested: int = 0
    tested: int = 0
    never_fired: int = 0
    too_few_episodes: int = 0
    no_out_of_sample: int = 0
    regime_dependent: int = 0
    episodes_replayed: int = 0
    trades_prevented: int = 0


class SkillTester:
    """Replays a skill's rules against real episodes, out of sample and hostile."""

    def __init__(
        self,
        minimum_episodes: int,
        minimum_firings: int,
        help_threshold: float,
        prior_help_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_firings < 1:
            raise ValueError(
                "a rule that never triggers is untested, not validated by the absence of "
                "counterexamples"
            )
        if not 0.0 < help_threshold < 1.0:
            raise ValueError("the help threshold is a rate inside (0, 1)")
        self._minimum_episodes = minimum_episodes
        self._minimum_firings = minimum_firings
        self._help_threshold = help_threshold
        self._prior_help_rate = prior_help_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._episodes: list = []
        self._rates: dict[tuple[str, str], RateEstimator] = {}
        self.standing = TesterStanding()

    def observe_episode(self, episode) -> None:
        """One real closed trade. Never an invented scenario (RL-063)."""
        self._episodes.append(episode)

    def test(self, skill, applies_to, distilled_at_ns: int | None = None) -> SkillBacktest:
        """One skill's rules replayed. `applies_to(episode) -> bool | None` is the rule."""
        self.standing.skills_tested += 1

        # Out of sample: a rule tested on the episodes it was distilled beside is
        # tested on its own training data, and the source may have been written
        # after looking at exactly those.
        cutoff = distilled_at_ns if distilled_at_ns is not None else 0
        episodes = [
            episode for episode in self._episodes if episode.closed_at_ns > cutoff
        ]
        self.standing.episodes_replayed += len(episodes)

        if distilled_at_ns is not None and not episodes and self._episodes:
            self.standing.no_out_of_sample += 1
            return self._backtest(
                skill.skill_id, NO_OUT_OF_SAMPLE, None, None, 0, 0, 0, 0, 0, {}, 0,
                f"all {len(self._episodes)} episode(s) predate this skill, so none is out of "
                f"sample. A rule tested on its own training data tells you nothing",
            )

        if len(episodes) < self._minimum_episodes:
            self.standing.too_few_episodes += 1
            return self._backtest(
                skill.skill_id, TOO_FEW_EPISODES, None, None, len(episodes), 0, 0, 0, 0, {},
                len(episodes),
                f"{len(episodes)} episode(s) of the {self._minimum_episodes} needed",
            )

        fired = 0
        prevented = 0
        prevented_losses = 0
        prevented_wins = 0
        by_regime: dict[str, dict] = {}

        for episode in episodes:
            verdict = applies_to(episode)
            if verdict is None:
                continue
            fired += 1
            regime = getattr(episode, "regime", "unknown")
            by_regime.setdefault(regime, {"fired": 0, "helped": 0})
            by_regime[regime]["fired"] += 1

            if verdict is False:
                # The rule would have stopped this trade. Both outcomes count:
                # a skill whose only effect is stopping losers is a good skill,
                # and one that only scores the trades it enabled would miss it.
                prevented += 1
                helped = not episode.was_profitable
                if episode.was_profitable:
                    prevented_wins += 1
                else:
                    prevented_losses += 1
            else:
                helped = episode.was_profitable

            self._rate_for(skill.skill_id, regime).observe(helped)
            if helped:
                by_regime[regime]["helped"] += 1

        self.standing.trades_prevented += prevented

        if fired < self._minimum_firings:
            # Untested, not validated. The two look identical in a naive score.
            self.standing.never_fired += 1
            return self._backtest(
                skill.skill_id, NEVER_FIRED, None, None, len(episodes), fired, prevented,
                prevented_losses, prevented_wins, by_regime, len(episodes),
                f"its rules fired on {fired} of {len(episodes)} episode(s), below the "
                f"{self._minimum_firings} needed. A rule that never triggers is untested, not "
                f"validated by the absence of counterexamples",
            )

        overall = self._rate_for(skill.skill_id, "all").estimate(self._minimum_firings)
        for regime in by_regime:
            estimate = self._rate_for(skill.skill_id, regime).estimate(self._minimum_firings)
            by_regime[regime]["help_rate"] = estimate.value
            by_regime[regime]["is_measured"] = estimate.is_fitted

        helping = [
            regime for regime, record in by_regime.items()
            if record.get("help_rate", 0.0) >= self._help_threshold
        ]
        hurting = [
            regime for regime, record in by_regime.items()
            if record.get("is_measured") and record.get("help_rate", 0.0) < self._help_threshold
        ]

        total_helped = sum(record["helped"] for record in by_regime.values())
        score = total_helped / fired if fired else 0.0

        if helping and hurting:
            verdict = REGIME_DEPENDENT
            self.standing.regime_dependent += 1
        elif score >= self._help_threshold:
            verdict = HELPED
        else:
            verdict = HURT

        self.standing.tested += 1
        return self._backtest(
            skill.skill_id, TESTED, verdict, score, len(episodes), fired, prevented,
            prevented_losses, prevented_wins, by_regime, len(episodes),
            f"its rules fired on {fired} of {len(episodes)} out-of-sample episode(s) and "
            f"would have helped {score:.0%} of the time"
            + (
                f", preventing {prevented} trade(s) -- {prevented_losses} that lost and "
                f"{prevented_wins} that won"
                if prevented
                else ""
            )
            + (
                f". It helps in {', '.join(sorted(helping))} and hurts in "
                f"{', '.join(sorted(hurting))}: reported that way rather than averaged, "
                f"because the average describes neither and the split is the usable finding"
                if verdict == REGIME_DEPENDENT
                else ""
            ),
        )

    def _rate_for(self, skill_id: str, regime: str) -> RateEstimator:
        key = (skill_id, regime)
        estimator = self._rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_help_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._rates[key] = estimator
        return estimator

    def _backtest(
        self, skill_id, state, verdict, score, episodes, fired, prevented,
        prevented_losses, prevented_wins, by_regime, out_of_sample, reason,
    ) -> SkillBacktest:
        return SkillBacktest(
            skill_id=skill_id,
            state=state,
            verdict=verdict,
            score=score,
            episodes_tested=episodes,
            times_it_fired=fired,
            trades_it_would_have_prevented=prevented,
            prevented_losses=prevented_losses,
            prevented_wins=prevented_wins,
            by_regime=by_regime,
            out_of_sample_episodes=out_of_sample,
            reason=reason,
            tested_at_ns=self._now_ns(),
        )


def describe_skill_testing(tester: SkillTester) -> dict:
    return {
        "part_id": PART_ID,
        "skills_tested": tester.standing.skills_tested,
        "tested": tester.standing.tested,
        "rules_that_never_fired": tester.standing.never_fired,
        "too_few_episodes": tester.standing.too_few_episodes,
        "no_out_of_sample_episodes": tester.standing.no_out_of_sample,
        "regime_dependent": tester.standing.regime_dependent,
        "episodes_replayed": tester.standing.episodes_replayed,
        "trades_that_would_have_been_prevented": tester.standing.trades_prevented,
        "tests_against_invented_scenarios": False,
    }


def run_skill_tester(
    tester: SkillTester, control_socket, read_skills_and_episodes, publish_backtests,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        publish_backtests(
            tuple(
                tester.test(skill, applies_to, distilled_at_ns)
                for skill, applies_to, distilled_at_ns in read_skills_and_episodes(tester)
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
