"""The placer reads fields its producers actually carry.

Both defects here crashed or silently misbehaved the first time a producer ran,
and neither could have been caught before then, because a field nobody supplies is
a field nobody reads wrongly.

`liquidation-cluster-mapper` and the volatility forecasters started on 2026-08-25
with phase 6. Within a minute `stop-target-placer` was restarting thirteen times a
minute on `AttributeError: 'LiquidationMap' object has no attribute
'cluster_prices'`, and its volatility read was returning None from a `getattr`
default for a field named `expected_move_fraction` that `VolatilityForecast` has
never had.

The second is the worse of the two. A crash announces itself; a `getattr` default
does not. Every stop this part placed was sized as though no volatility forecast
existed, and nothing anywhere would have said so.

These tests build the real producer types, so a field rename on either side fails
here rather than in a live process at three in the morning.
"""

from __future__ import annotations

import pytest

from parts.prediction.liquidation_cluster_mapper import LiquidationCluster, LiquidationMap
from parts.risk_capital_allocation.stop_target_placer import StopTargetPlacer


def cluster(price: float, side: str = "below", venue="binance-usdm", symbol="BTCUSDT"):
    return LiquidationCluster(
        venue_id=venue, symbol=symbol, price=price, notional=1_000_000.0,
        side=side, leverage_band=10.0, distance_fraction=0.02, confidence=None,
    )


def test_a_liquidation_map_carries_clusters_not_cluster_prices():
    """The field the placer crashed on, pinned on the producer's own type."""
    assert not hasattr(LiquidationMap, "cluster_prices")
    assert "clusters" in LiquidationMap.__dataclass_fields__
    assert "price" in LiquidationCluster.__dataclass_fields__


def test_a_volatility_forecast_carries_expected_volatility():
    """The field the placer was silently defaulting away from."""
    from runtime.forecast_types import VolatilityForecast

    assert "expected_volatility" in VolatilityForecast.__dataclass_fields__
    assert "expected_move_fraction" not in VolatilityForecast.__dataclass_fields__


def test_the_placer_takes_prices_out_of_a_real_map():
    """The read the live part performs, against the type the producer emits."""
    mapped = LiquidationMap(
        venue_id="binance-usdm", symbol="BTCUSDT", state="mapped",
        mark_price=80_000.0,
        clusters=(cluster(78_000.0), cluster(76_000.0), cluster(82_000.0, side="above")),
        open_interest_notional=5_000_000.0, largest_cluster=None,
        reason="three bands estimated", mapped_at_ns=1,
    )
    assert mapped.is_usable

    placer = StopTargetPlacer.__new__(StopTargetPlacer)
    placer._clusters = {}
    placer.set_liquidation_clusters(
        mapped.venue_id, mapped.symbol, tuple(c.price for c in mapped.clusters)
    )

    assert placer._clusters[("binance-usdm", "BTCUSDT")] == (76_000.0, 78_000.0, 82_000.0)


def test_an_unmapped_map_is_not_used():
    """Its clusters would be a guess wearing the shape of a measurement."""
    unmapped = LiquidationMap(
        venue_id="binance-usdm", symbol="BTCUSDT", state="not-enough-open-interest",
        mark_price=80_000.0, clusters=(), open_interest_notional=0.0,
        largest_cluster=None, reason="no open interest reported", mapped_at_ns=1,
    )
    assert not unmapped.is_usable


@pytest.mark.parametrize("field_name", ["venue_id", "symbol", "clusters", "state"])
def test_every_field_the_placer_reads_from_a_map_exists(field_name):
    assert field_name in LiquidationMap.__dataclass_fields__
