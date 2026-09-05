

"""The pair budget: every pair measured, but not every pair on every pass.

The mapper's own behaviour is covered by the intelligence block test beside this
file. What is covered here is the budget added on 2026-09-05, when the
cash-equity universe went from 17 tracked underlyings to 2,444 shares.

Measured on this box that day: one pair costs 76 microseconds, so 2,985,346
pairs are 228 seconds of CPU for one pass against a 15-second remap interval --
fifteen cores, continuously, for a statistic whose own window is an hour long.

The properties: a pass costs what it was budgeted, a sweep still reaches every
pair, and the answer is the same one measuring everything gives.
"""

from parts.intelligence.correlation_cluster_mapper import CorrelationClusterMapper


def a_budgeted_mapper(passes=4, **extra):
    return CorrelationClusterMapper(
        window_length=32,
        minimum_shared_observations=8,
        cluster_threshold=0.8,
        passes_per_sweep_ceiling=passes,
        remembered_pairs_maximum=1000,
        **extra,
    )


def feed_a_family(mapper, correlated=6, independent=6, steps=32):
    """One family that really moves together, plus symbols that do not."""
    import random

    random.seed(5)
    at = 1_000_000_000
    for step in range(steps):
        shock = random.gauss(0, 1)
        for index in range(correlated):
            mapper.observe_price(f"C{index}", 100 + 5 * shock, at + step * 1_000_000_000)
        for index in range(independent):
            mapper.observe_price(
                f"I{index}", 100 + 5 * random.gauss(0, 1), at + step * 1_000_000_000
            )


def test_a_pass_measures_only_its_budget_and_says_how_many_it_did_not_reach():
    mapper = a_budgeted_mapper(passes=4)
    feed_a_family(mapper)

    mapper.map()

    standing = mapper.standing
    assert standing.pairs_total == 66
    assert standing.passes_per_sweep == 4
    assert standing.pairs_measured_this_pass == 17
    assert standing.pairs_not_reached_this_pass == 49
    # A pair not reached this pass is a fact about the machine; `unmeasured_pairs`
    # is a fact about the market. They must never be one number.
    assert standing.pairs_not_reached_this_pass != standing.unmeasured_pairs


def test_a_sweep_reaches_every_pair_and_counts_itself():
    mapper = a_budgeted_mapper(passes=4)
    feed_a_family(mapper)

    for _ in range(4):
        mapper.map()

    assert mapper.standing.sweeps_completed == 1
    assert mapper.standing.pairs_measured_this_sweep == 0  # the next sweep starts fresh


def test_the_budget_finds_the_same_cluster_as_measuring_everything():
    """The budget must cost time, not the answer."""
    everything = CorrelationClusterMapper(
        window_length=32, minimum_shared_observations=8, cluster_threshold=0.8,
    )
    feed_a_family(everything)
    whole = {tuple(cluster.symbols) for cluster in everything.map() if len(cluster.symbols) > 1}

    budgeted = a_budgeted_mapper(passes=4)
    feed_a_family(budgeted)
    for _ in range(4):
        clusters = budgeted.map()

    assert {tuple(c.symbols) for c in clusters if len(c.symbols) > 1} == whole


def test_a_pair_that_stops_correlating_is_forgotten_when_it_is_remeasured():
    """A remembered correlation that never expired would keep a cluster together
    long after the correlation that formed it was gone."""
    mapper = a_budgeted_mapper(passes=1)
    feed_a_family(mapper, correlated=4, independent=0)
    mapper.map()
    assert mapper.standing.strong_pairs_remembered > 0

    # The same symbols, now moving independently.
    import random

    random.seed(9)
    at = 100_000_000_000
    for step in range(64):
        for index in range(4):
            mapper.observe_price(
                f"C{index}", 100 + 5 * random.gauss(0, 1), at + step * 1_000_000_000
            )
    mapper.map()

    assert mapper.standing.strong_pairs_remembered == 0


def test_remembering_is_bounded_and_the_eviction_is_counted():
    mapper = CorrelationClusterMapper(
        window_length=32, minimum_shared_observations=8, cluster_threshold=0.1,
        remembered_pairs_maximum=5,
    )
    feed_a_family(mapper, correlated=8, independent=0)

    mapper.map()

    assert mapper.standing.strong_pairs_remembered <= 5
    assert mapper.standing.strong_pairs_evicted > 0


def test_a_mapper_given_no_budget_still_measures_every_pair():
    """What a universe small enough for it states, and what this part did before."""
    mapper = CorrelationClusterMapper(
        window_length=32, minimum_shared_observations=8, cluster_threshold=0.8,
    )
    feed_a_family(mapper)

    mapper.map()

    assert mapper.standing.pairs_measured_this_pass == mapper.standing.pairs_total
    assert mapper.standing.pairs_not_reached_this_pass == 0
    assert mapper.standing.sweeps_completed == 1
