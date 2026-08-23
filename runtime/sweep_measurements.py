"""The measurements a watch condition may name, and how each is computed.

Shared vocabulary between watch-condition-compiler (which refuses a condition
naming a measurement nobody computes) and universal-symbol-sweeper (which
computes them per symbol from what crosses the bus). One list, so a condition
compiled against it is a condition the sweeper can evaluate -- the compiler
never imports the sweeper, nor the sweeper the compiler (T-4).
"""

from __future__ import annotations

PRICE = "price"
RETURN_OVER_WINDOW = "return_over_window"
CONSOLIDATED_PRICE = "consolidated_price"
VENUE_DISAGREEMENT = "venue_disagreement"

KNOWN_MEASUREMENTS = (PRICE, RETURN_OVER_WINDOW, CONSOLIDATED_PRICE, VENUE_DISAGREEMENT)


def measure(latest_price: float | None, window_open_price: float | None, consolidated: float | None) -> dict:
    """Every measurement that can be stated from these inputs; absent ones are left out."""
    found: dict[str, float] = {}
    if latest_price is not None:
        found[PRICE] = latest_price
        if window_open_price:
            found[RETURN_OVER_WINDOW] = latest_price / window_open_price - 1.0
        if consolidated:
            found[CONSOLIDATED_PRICE] = consolidated
            found[VENUE_DISAGREEMENT] = latest_price / consolidated - 1.0
    return found


__all__ = ["KNOWN_MEASUREMENTS", "PRICE", "RETURN_OVER_WINDOW", "CONSOLIDATED_PRICE", "VENUE_DISAGREEMENT", "measure"]
