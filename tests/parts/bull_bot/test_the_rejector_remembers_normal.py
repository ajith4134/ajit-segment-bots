"""What normal looks like survives a restart.

Measured on the live spine on 2026-08-25, after each of the day's restarts:

    bull-outlier-rejector   3 features with a learned normal, 56 of 56 vectors
                            unjudgeable
    bull-conviction-model   67 refused as out of distribution, 0 convictions

An outlier rejector that begins each process knowing nothing calls every vector
unrecognisable, and the conviction model refuses an unrecognisable vector by
design. So the bot could not form a single opinion until the normals had been
learned again -- and every restart put it back to the beginning, which on this
project's own record is several times a day.

The moments were already checkpointable: `RunningMoments.state` and
`restore_state` were written for this and nothing called them.
"""

from __future__ import annotations

import pytest

from parts.bull_bot.bull_outlier_rejector import (
    COMPONENT, BullOutlierRejector, restore_or_start_cold,
)
from runtime.bot_opinion import FeatureVector
from runtime.learned_state import LearnedStateStore


def rejector() -> BullOutlierRejector:
    return BullOutlierRejector(
        deviation_threshold=3.0, minimum_observations=5,
        half_life_observations=100.0, maximum_unjudgeable_fraction=0.5,
        now_ns=lambda: 1,
    )


def vector(value: float) -> FeatureVector:
    return FeatureVector(
        bot="bull", venue_id="binance-usdm", symbol="BTCUSDT",
        features={"price_z_score": value, "spread_fraction": value / 10},
        missing=(), sources={}, built_at_ns=1,
    )


def test_a_rejector_that_has_seen_nothing_cannot_judge_anything():
    """The state every restart used to begin in."""
    cold = rejector()
    flag = cold.judge(vector(1.0))
    assert flag.is_out_of_distribution
    assert cold.standing.unjudgeable_vectors == 1


def test_the_normals_come_back_after_a_restart(durable_tmp_path):
    store = LearnedStateStore(durable_tmp_path / "learned")
    store.root.mkdir(parents=True, exist_ok=True)

    learning = rejector()
    for step in range(40):
        learning.observe_vector(vector(1.0 + (step % 5) / 10))
    store.save("bull-outlier-rejector", COMPONENT, learning.state(), learning.learned_settings())

    restarted = rejector()
    restore_or_start_cold(restarted, store)

    assert restarted.standing.checkpoint_saved_at_ns is not None
    assert len(restarted._moments) == 2, "both features came back"
    flag = restarted.judge(vector(1.2))
    assert not flag.is_out_of_distribution, "an ordinary vector is recognised, not refused"


def test_a_checkpoint_learned_under_other_settings_is_refused_not_reinterpreted(durable_tmp_path):
    """Moments decayed at one half-life and read at another describe nothing real."""
    store = LearnedStateStore(durable_tmp_path / "learned")
    store.root.mkdir(parents=True, exist_ok=True)

    learning = rejector()
    for step in range(40):
        learning.observe_vector(vector(1.0 + (step % 5) / 10))
    store.save("bull-outlier-rejector", COMPONENT, learning.state(), learning.learned_settings())

    different = BullOutlierRejector(
        deviation_threshold=3.0, minimum_observations=5,
        half_life_observations=7.0, maximum_unjudgeable_fraction=0.5, now_ns=lambda: 1,
    )
    restore_or_start_cold(different, store)
    assert not different._moments, "the normals were not adopted under a different half-life"
    assert different.standing.checkpoint_verdict is not None


def test_the_observation_count_is_the_best_observed_feature():
    """A total across features would make one busy feature look like progress."""
    one = rejector()
    for _ in range(9):
        one.observe_vector(vector(1.0))
    assert one.training_observations == 9
