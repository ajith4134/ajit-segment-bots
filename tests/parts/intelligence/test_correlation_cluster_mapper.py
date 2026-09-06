

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


# ---- the flat series, and the standing read that remapped -------------------
#
# Both of these crash-looped this part on the live spine on 2026-09-06: 139
# restarts, exit code 1, `TypeError: bad operand type for abs(): 'NoneType'`
# raised out of `read_standing`. The None is the first defect; that it was
# raised out of a standing read at all is the second.

import pathlib

FLAT_RUN_DAY = "2026-09-04"
# HINDUNILVR really printed 1972.0 thirty consecutive times between 09:53:53 and
# 09:54:34 IST that day, while MARUTI traded through the same forty-one seconds.
# Found by scanning the tape, not chosen: it is the longest flat run any NSE_EQ
# instrument on the tape has, and a quiet share standing still for forty seconds
# is the ordinary case on this market rather than a corner of it.
FLAT_SYMBOL = "HINDUNILVR"
MOVING_SYMBOL = "MARUTI"


def real_prints_for(symbols, day=FLAT_RUN_DAY):
    """Real Upstox prints off this project's own tape, per symbol (RL-063).

    Deduplicated exactly as `RollingWindow.observe` does -- the same price at the
    same instant is one fact redelivered -- so what this returns is what the
    mapper's windows would actually have held.
    """
    from tests.conftest import upstox_trades_for

    root = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
    keys = [
        entry.name
        for entry in root.iterdir()
        if entry.name.startswith("NSE_EQ") and (entry / f"{day}.index").exists()
    ]
    series: dict[str, list] = {symbol: [] for symbol in symbols}
    for trade in upstox_trades_for(day, keys, 100_000):
        if trade.symbol in series:
            observation = (trade.price, trade.venue_time_ns)
            if not series[trade.symbol] or series[trade.symbol][-1] != observation:
                series[trade.symbol].append(observation)
    return series


def longest_flat_run_in(observations):
    """Where the longest stretch of one unchanging price ends, and how long it is."""
    longest, ends_at, run = 1, 0, 1
    for index in range(1, len(observations)):
        run = run + 1 if observations[index][0] == observations[index - 1][0] else 1
        if run > longest:
            longest, ends_at = run, index
    return longest, ends_at


def test_a_symbol_that_did_not_move_leaves_its_pairs_unmeasured_not_crashing():
    """Correlation is undefined when a series has no variation. Not zero, and not a crash."""
    import pytest

    series = real_prints_for((FLAT_SYMBOL, MOVING_SYMBOL))
    if not series[FLAT_SYMBOL] or not series[MOVING_SYMBOL]:
        pytest.skip(f"the tape holds no {FLAT_RUN_DAY} prints for both symbols")

    flat = series[FLAT_SYMBOL]
    run_length, run_ends_at = longest_flat_run_in(flat)
    window = 16
    assert run_length > window, (
        f"{FLAT_SYMBOL} moved within every {window}-observation window on "
        f"{FLAT_RUN_DAY}, so this test would prove nothing"
    )

    mapper = CorrelationClusterMapper(
        window_length=window,
        minimum_shared_observations=8,
        cluster_threshold=0.8,
        passes_per_sweep_ceiling=1,
        remembered_pairs_maximum=1000,
    )
    # Both symbols fed up to the end of the real flat run, so the flat symbol's
    # window holds one price and the other's holds a share that really moved.
    until_ns = flat[run_ends_at][1]
    for symbol, observations in series.items():
        for price, at_ns in observations:
            if at_ns <= until_ns:
                mapper.observe_price(symbol, price, at_ns)

    mapper.map()

    standing = mapper.standing
    # A pair whose symbol never moved has all the shared history it needs and no
    # variation to correlate. Counted apart from `unmeasured_pairs`, which is
    # about history, and never as a correlation of zero -- zero says "measured,
    # and they are unrelated", which is a claim nobody made.
    assert standing.pairs_without_variation == 1
    assert standing.unmeasured_pairs == 0


def test_reading_the_standing_does_not_remap():
    """The mapping is paced; a standing read that remapped would pay for all of it.

    `read_standing` is called on every health emit -- once a second -- so a
    standing read calling `map()` recomputed a quadratic statistic once a second
    whatever `correlation_remap_interval_seconds` said, and advanced the sweep
    cursor while doing it.
    """
    from parts.intelligence.correlation_cluster_mapper import (
        describe_correlation_clusters,
    )

    mapper = a_budgeted_mapper(passes=1)
    feed_a_family(mapper)
    clusters = mapper.map()
    mappings = mapper.standing.mappings

    described = describe_correlation_clusters(mapper)

    assert mapper.standing.mappings == mappings, "the standing read remapped"
    assert described["clusters_now"] == len(clusters), "it must report the last mapping"
