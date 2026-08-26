"""The vocabulary discovery and scanning share.

Every price here comes from real captured Binance aggTrade frames (RL-063). A
vocabulary tested on a generated sine wave would pass on a market that does not
exist -- and the whole point of these measurements is that a threshold placed on
one of them means something about the market a symbol is actually in.
"""

import pytest

from runtime.rolling_statistics import RollingWindow
from runtime.sweep_measurements import (
    CROSS_SECTIONAL_MEASUREMENTS,
    DRAWDOWN_FROM_HIGH,
    KNOWN_MEASUREMENTS,
    MARKET_EXCESS_RANK,
    MARKET_EXCESS_RETURN,
    PER_SYMBOL_MEASUREMENTS,
    POSITION_IN_RANGE,
    REALISED_VOLATILITY,
    RETURN_OVER_WINDOW,
    RETURN_RANK,
    RETURN_Z,
    RUNUP_FROM_LOW,
    VOLATILITY_RATIO,
    add_cross_sectional,
    measure_symbol,
)

VENUE = "binance-usdm"
CAPTURED_RUN = "2026-08-22-btcusdt-aggtrade-run.jsonl"


@pytest.fixture
def real_prices(read_captured_payloads):
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, CAPTURED_RUN)
    prices = [trade.price for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(prices) > 100, "the captured run must hold enough prices to fill a window"
    return prices


def a_window(prices, length=64):
    window = RollingWindow(length=length)
    for price in prices:
        window.observe(price)
    return window


def test_the_vocabulary_has_no_duplicate_names():
    """The compiler refuses a measurement nobody computes; two of one name hides that."""
    assert len(set(KNOWN_MEASUREMENTS)) == len(KNOWN_MEASUREMENTS)
    assert set(KNOWN_MEASUREMENTS) == set(PER_SYMBOL_MEASUREMENTS) | set(CROSS_SECTIONAL_MEASUREMENTS)
    assert not set(PER_SYMBOL_MEASUREMENTS) & set(CROSS_SECTIONAL_MEASUREMENTS)


def test_every_measurement_stated_is_in_the_vocabulary(real_prices):
    """A name the sweeper computes but the compiler will not accept is unreachable."""
    found = measure_symbol(a_window(real_prices[:64]), minimum_observations=2)
    assert found
    assert set(found) <= set(KNOWN_MEASUREMENTS)


def test_a_window_too_short_states_nothing_it_cannot_support():
    """Absent, never zero -- the sweeper counts the symbol unmeasurable instead."""
    window = RollingWindow(length=64)
    window.observe(100.0)
    found = measure_symbol(window, minimum_observations=32)
    assert RETURN_Z not in found
    assert REALISED_VOLATILITY not in found
    assert VOLATILITY_RATIO not in found


def test_the_time_series_measurements_describe_the_real_series(real_prices):
    prices = real_prices[:128]
    found = measure_symbol(a_window(prices, length=128), minimum_observations=2)

    assert found[RETURN_OVER_WINDOW] == pytest.approx(prices[-1] / prices[0] - 1.0)
    assert found[REALISED_VOLATILITY] > 0.0
    assert 0.0 <= found[POSITION_IN_RANGE] <= 1.0
    # A drawdown from the window high is never positive and a run-up from the low
    # is never negative; the pair is what says where in its own range price sits.
    assert found[DRAWDOWN_FROM_HIGH] <= 0.0
    assert found[RUNUP_FROM_LOW] >= 0.0


def test_the_return_is_expressed_in_the_symbols_own_volatility(real_prices):
    """A 2% move means different things in a stablecoin pair and a thin alt."""
    found = measure_symbol(a_window(real_prices[:128], length=128), minimum_observations=2)
    assert found[RETURN_Z] == pytest.approx(
        found[RETURN_OVER_WINDOW] / found[REALISED_VOLATILITY]
    )


def test_the_market_is_subtracted_before_anything_is_ranked(real_prices):
    """Ranking raw returns in a one-factor market ranks beta, not dislocation."""
    measured = {
        ("v", "a"): {RETURN_OVER_WINDOW: 0.05},
        ("v", "b"): {RETURN_OVER_WINDOW: 0.03},
        ("v", "c"): {RETURN_OVER_WINDOW: 0.01},
    }
    add_cross_sectional(measured, minimum_symbols=2)

    # The median of the three is 0.03, so the middle symbol did exactly what the
    # market did and its excess is zero however large its raw return was.
    assert measured[("v", "b")][MARKET_EXCESS_RETURN] == pytest.approx(0.0)
    assert measured[("v", "a")][MARKET_EXCESS_RETURN] == pytest.approx(0.02)
    assert measured[("v", "c")][MARKET_EXCESS_RETURN] == pytest.approx(-0.02)


def test_a_rank_is_a_percentile_of_the_symbols_that_have_the_measurement():
    measured = {
        ("v", str(index)): {RETURN_OVER_WINDOW: float(index)}
        for index in range(5)
    }
    add_cross_sectional(measured, minimum_symbols=2)

    assert measured[("v", "0")][RETURN_RANK] == pytest.approx(0.0)
    assert measured[("v", "4")][RETURN_RANK] == pytest.approx(1.0)
    assert measured[("v", "2")][RETURN_RANK] == pytest.approx(0.5)


def test_ties_share_a_rank_rather_than_being_ordered_by_arrival():
    """Otherwise two identical returns are separated by an accident of iteration."""
    measured = {
        ("v", "a"): {RETURN_OVER_WINDOW: 0.01},
        ("v", "b"): {RETURN_OVER_WINDOW: 0.01},
        ("v", "c"): {RETURN_OVER_WINDOW: 0.09},
    }
    add_cross_sectional(measured, minimum_symbols=2)
    assert measured[("v", "a")][RETURN_RANK] == measured[("v", "b")][RETURN_RANK]


def test_below_the_minimum_no_cross_sectional_claim_is_made():
    """A percentile over one observation is 0 or 1 by construction and says nothing."""
    measured = {("v", "a"): {RETURN_OVER_WINDOW: 0.05}}
    add_cross_sectional(measured, minimum_symbols=8)
    assert RETURN_RANK not in measured[("v", "a")]
    assert MARKET_EXCESS_RETURN not in measured[("v", "a")]


def test_a_symbol_missing_the_source_measurement_is_left_out_of_its_rank():
    """Ranked against the symbols that have it, never given a default position."""
    measured = {
        ("v", "a"): {RETURN_OVER_WINDOW: 0.05},
        ("v", "b"): {RETURN_OVER_WINDOW: 0.01},
        ("v", "c"): {},
    }
    add_cross_sectional(measured, minimum_symbols=2)
    assert RETURN_RANK not in measured[("v", "c")]
    assert MARKET_EXCESS_RANK in measured[("v", "a")]


def test_the_whole_vocabulary_is_reachable_from_a_real_universe(real_prices):
    """Every name the compiler accepts must be one the sweeper can actually state.

    A measurement in KNOWN_MEASUREMENTS that nothing ever computes is a condition
    that compiles and then silently never fires -- which is exactly the failure
    the compiler's refusal exists to prevent, one layer along.
    """
    measured = {}
    for index in range(8):
        # Eight overlapping slices of the same real run: different windows of one
        # market rather than eight invented series.
        slice_start = index * 8
        prices = real_prices[slice_start:slice_start + 128]
        measured[("binance-usdm", f"S{index}")] = measure_symbol(
            a_window(prices, length=128),
            consolidated=prices[len(prices) // 2],
            minimum_observations=2,
        )
    add_cross_sectional(measured, minimum_symbols=8)

    stated = set()
    for found in measured.values():
        stated |= set(found)
    missing = set(KNOWN_MEASUREMENTS) - stated
    assert not missing, f"never computed from a real universe: {sorted(missing)}"
