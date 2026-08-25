"""The limiter measures exposure from what a position actually carries.

Until 2026-08-25 it read `getattr(position, "exposure_fraction", 0.0)` -- a field
`Position` has never had -- so every position was observed at zero exposure, the
book summed to zero, and the limit it published was the full per-position cap on
every tick since it first ran. A limiter that cannot lower a limit is not a
limiter, and nothing on any board said so.
"""

from __future__ import annotations

import pytest

from parts.risk_capital_allocation.exposure_limiter import ExposureLimiter
from runtime.trading_types import Position


def position(symbol: str, quantity: float, entry: float) -> Position:
    return Position(
        venue_id="binance-usdm", symbol=symbol, quantity=quantity,
        average_entry_price=entry, realised_pnl=0.0, fees_paid=0.0,
        opened_at_ns=1, updated_at_ns=2,
    )


def limiter() -> ExposureLimiter:
    return ExposureLimiter(
        maximum_per_position_fraction=0.10,
        maximum_total_fraction=0.30,
        maximum_per_cluster_fraction=0.20,
        now_ns=lambda: 1,
    )


def test_a_position_carries_a_quantity_and_a_price_and_never_a_fraction():
    assert not hasattr(Position, "exposure_fraction")
    assert {"quantity", "average_entry_price"} <= set(Position.__dataclass_fields__)


def test_the_book_lowers_the_limit_once_a_balance_is_known():
    one = limiter()
    one.set_allotment(10_000.0)
    held = position("BTCUSDT", 0.02, 100_000.0)  # 2,000 of 10,000 -- a fifth of the book
    one.observe_position(held.venue_id, held.symbol, abs(held.quantity) * held.average_entry_price)

    limit = one.read_limit("ETHUSDT")
    assert limit.fraction_of_allotment == pytest.approx(0.30 - 0.20)
    assert one.standing.positions_without_a_balance == 0


def test_a_position_seen_before_any_balance_is_unknown_and_not_free():
    """Zero exposure would be a measurement nobody made."""
    one = limiter()
    one.observe_position("binance-usdm", "BTCUSDT", 2_000.0)
    assert one.standing.positions_without_a_balance == 1
    assert one.total_exposure == 0.0

    one.set_allotment(10_000.0)
    assert one.total_exposure == pytest.approx(0.20)


def test_a_changed_allotment_re_measures_the_book():
    """The caps are fractions, so the same book is a different exposure."""
    one = limiter()
    one.set_allotment(10_000.0)
    one.observe_position("binance-usdm", "BTCUSDT", 2_000.0)
    assert one.total_exposure == pytest.approx(0.20)

    one.set_allotment(20_000.0)
    assert one.total_exposure == pytest.approx(0.10)


def test_a_closed_position_stops_using_the_book():
    one = limiter()
    one.set_allotment(10_000.0)
    one.observe_position("binance-usdm", "BTCUSDT", 2_000.0)
    one.observe_position("binance-usdm", "BTCUSDT", 0.0)
    assert one.total_exposure == 0.0
