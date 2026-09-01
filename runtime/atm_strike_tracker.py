"""Which option contract is currently closest to at-the-money, per underlying.

"At the money" means delta closest to 0.5 (docs/superpowers/specs/
2026-09-01-options-segment-bots-design.md section 3) -- a structural fact
about what ATM means for a call (a put's ATM delta is closest to -0.5), not
a settings value (RL-061). A settings-driven delta target is the spec's own
named future upgrade, not built here.

Resolves contracts to their underlying via broker-instrument-listing, the
same two-pass pattern runtime/underlying_open_interest.py uses (an option's
listing names its underlying's instrument_key; only the underlying's own
listing carries the trading_symbol this tracker is asked about) -- T-4, no
hardcoded key format assumed.

Nearest expiry only (goal.md item 3): a contract whose expiry has already
passed, or is not the soonest future one known for its underlying, is never
returned as the ATM choice, even if its delta happens to be closer to 0.5
than the nearest expiry's own strikes -- an intent this session cannot
carry past today's close has no business being expressed by a contract two
weeks out.
"""

from __future__ import annotations

from dataclasses import dataclass

CALL = "CE"
PUT = "PE"
# A put's delta is negative by convention; distance from being at-the-money
# is measured against -0.5 for a put and +0.5 for a call, both structural
# facts about how delta is signed, not settings.
ATM_TARGET_DELTA = {CALL: 0.5, PUT: -0.5}


@dataclass(frozen=True)
class AtmChoice:
    """The contract currently closest to 0.5 delta for one underlying and side."""

    instrument_key: str
    trading_symbol: str
    strike_price: float
    expiry_ms: int
    delta: float
    distance_from_atm: float


class AtmStrikeTracker:
    """Tracks every known option contract's delta and reports the ATM one."""

    def __init__(self) -> None:
        self._underlying_symbol_by_key: dict[str, str] = {}
        # instrument_key -> (underlying_symbol, instrument_type, trading_symbol, strike, expiry_ms)
        self._contracts: dict[str, tuple] = {}
        self._pending_contracts: dict[str, tuple] = {}
        self._latest_delta: dict[str, float] = {}

    def observe_listing(self, listing) -> None:
        """One instrument listing -- either an underlying itself, or an option contract on one."""
        if listing.underlying_key is None:
            self._underlying_symbol_by_key[listing.instrument_key] = listing.trading_symbol
            for key, pending in list(self._pending_contracts.items()):
                if pending[0] == listing.instrument_key:
                    self._contracts[key] = (listing.trading_symbol, *pending[1:])
                    del self._pending_contracts[key]
            return
        if listing.instrument_type not in (CALL, PUT):
            return
        symbol = self._underlying_symbol_by_key.get(listing.underlying_key)
        record = (
            symbol, listing.instrument_type, listing.trading_symbol,
            listing.strike_price, listing.expiry_ms,
        )
        if symbol is None:
            self._pending_contracts[listing.instrument_key] = (
                listing.underlying_key, listing.instrument_type, listing.trading_symbol,
                listing.strike_price, listing.expiry_ms,
            )
        else:
            self._contracts[listing.instrument_key] = record

    def observe_greeks(self, greeks) -> None:
        """One contract's latest delta reading, replacing its prior one."""
        self._latest_delta[greeks.instrument_key] = greeks.delta

    def underlying_of(self, instrument_key: str) -> str | None:
        """Which underlying this contract belongs to, or None if not yet
        resolved -- so a caller reacting to a greeks update (which carries
        only an instrument_key) knows which underlying's ATM pick to
        re-derive, without waiting for the next listing refresh. Real
        listings refresh roughly daily; greeks update continuously, and an
        ATM pick that only moved with the listing feed would be frozen for
        the whole trading day."""
        record = self._contracts.get(instrument_key)
        return record[0] if record is not None else None

    def atm_call_for(self, underlying_symbol: str, now_ms: int | None = None) -> AtmChoice | None:
        return self._atm_for(underlying_symbol, CALL, now_ms)

    def atm_put_for(self, underlying_symbol: str, now_ms: int | None = None) -> AtmChoice | None:
        return self._atm_for(underlying_symbol, PUT, now_ms)

    def _atm_for(self, underlying_symbol: str, side: str, now_ms: int | None) -> AtmChoice | None:
        candidates = [
            (key, record)
            for key, record in self._contracts.items()
            if record[0] == underlying_symbol and record[1] == side and key in self._latest_delta
        ]
        if not candidates:
            return None
        if now_ms is not None:
            candidates = [c for c in candidates if c[1][4] is not None and c[1][4] > now_ms]
        if not candidates:
            return None
        nearest_expiry = min(record[4] for _, record in candidates)
        candidates = [c for c in candidates if c[1][4] == nearest_expiry]

        target = ATM_TARGET_DELTA[side]
        key, record = min(
            candidates, key=lambda item: abs(self._latest_delta[item[0]] - target)
        )
        _, _, trading_symbol, strike, expiry_ms = record
        delta = self._latest_delta[key]
        return AtmChoice(
            instrument_key=key, trading_symbol=trading_symbol, strike_price=strike,
            expiry_ms=expiry_ms, delta=delta, distance_from_atm=abs(delta - target),
        )


__all__ = ["ATM_TARGET_DELTA", "AtmChoice", "AtmStrikeTracker", "CALL", "PUT"]
