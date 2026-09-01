"""Sums a broker's per-contract open interest into one figure per underlying.

`broker-open-interest` is published per *option contract* (one instrument_key
per strike/expiry), but a feature builder reasons about the underlying a
candidate is on (docs/superpowers/specs/2026-09-01-options-segment-bots-design.md
section 4: a detector's job is to spot a condition on the thing that moves).
This is the one place that resolution happens, the same reasoning
`runtime/price_frames.py` already uses for price: do it once, not in every
feature builder that needs it.

Resolution goes through `broker-instrument-listing`, never a hardcoded key --
`underlying_key` on an option's own listing names its underlying's
instrument_key, and only that underlying's own listing carries the
trading_symbol a feature builder's candidate.symbol actually is (T-4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnderlyingOpenInterestTotals:
    """Open interest and order flow, summed across one underlying's option chain.

    `observed_at_ns` is the freshest contract's own broker_time_ns among the
    ones summed, never the moment this aggregator was asked -- a reader that
    windows this value needs the market's own time for it, the same reason a
    frame's own price levels carry their observation time rather than the
    frame's publish time (runtime/price_frames.py).
    """

    open_interest: float
    total_buy_quantity: float
    total_sell_quantity: float
    observed_at_ns: int


class UnderlyingOpenInterestAggregator:
    """Resolves option contracts to their underlying and keeps a running sum."""

    def __init__(self) -> None:
        # instrument_key -> underlying's own trading_symbol, learned from
        # broker-instrument-listing. Populated in two passes because an
        # option's listing names its underlying by instrument_key, and only
        # the underlying's own listing (a different row) carries the
        # trading_symbol a candidate's own `symbol` field actually is.
        self._underlying_symbol_by_key: dict[str, str] = {}
        self._contract_underlying: dict[str, str] = {}
        self._pending_contracts: dict[str, str] = {}
        self._latest_by_contract: dict[str, tuple[float, float, float, int]] = {}

    def observe_listing(self, listing) -> None:
        """One instrument listing -- either an underlying itself, or a contract on one."""
        if listing.underlying_key is None:
            # This listing IS an underlying (or has no options concept at
            # all, like a plain equity) -- record its own symbol, and
            # resolve any contracts already seen that were waiting on it.
            self._underlying_symbol_by_key[listing.instrument_key] = listing.trading_symbol
            for contract_key, underlying_key in list(self._pending_contracts.items()):
                if underlying_key == listing.instrument_key:
                    self._contract_underlying[contract_key] = listing.trading_symbol
                    del self._pending_contracts[contract_key]
        else:
            symbol = self._underlying_symbol_by_key.get(listing.underlying_key)
            if symbol is None:
                # The underlying's own listing has not arrived yet -- keep
                # the contract's underlying_key and resolve it the moment it
                # does, rather than dropping the contract on the floor.
                self._pending_contracts[listing.instrument_key] = listing.underlying_key
            else:
                self._contract_underlying[listing.instrument_key] = symbol

    def observe_open_interest(self, reading) -> None:
        """One contract's latest open-interest reading, replacing its prior one."""
        self._latest_by_contract[reading.instrument_key] = (
            reading.open_interest, reading.total_buy_quantity, reading.total_sell_quantity,
            reading.broker_time_ns,
        )

    def totals_for(self, underlying_symbol: str) -> UnderlyingOpenInterestTotals | None:
        """The summed chain for one underlying, or None if nothing is known yet.

        None is not zero: an underlying with no open interest observed yet is a
        different fact from one whose whole chain is genuinely flat (Rule 8).
        """
        contract_keys = [
            key for key, symbol in self._contract_underlying.items()
            if symbol == underlying_symbol
        ]
        readings = [self._latest_by_contract[key] for key in contract_keys if key in self._latest_by_contract]
        if not readings:
            return None
        return UnderlyingOpenInterestTotals(
            open_interest=sum(oi for oi, _, _, _ in readings),
            total_buy_quantity=sum(buy for _, buy, _, _ in readings),
            total_sell_quantity=sum(sell for _, _, sell, _ in readings),
            observed_at_ns=max(at_ns for _, _, _, at_ns in readings),
        )


__all__ = ["UnderlyingOpenInterestAggregator", "UnderlyingOpenInterestTotals"]
