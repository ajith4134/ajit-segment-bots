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
        # trading_symbol -> instrument_key, so a caller holding only the name a
        # contract trades under can ask what it is a claim on. Every other part
        # in this system names an instrument by its trading symbol -- an intent
        # says "NIFTY 24000 CE 08 SEP 26", never an Upstox instrument_key -- and
        # without this the answer existed here and could not be asked for.
        self._key_by_trading_symbol: dict[str, str] = {}
        self._latest_delta: dict[str, float] = {}
        # Contract keys grouped by the underlying and side they belong to, so
        # `_atm_for` asks for one underlying's ladder instead of filtering every
        # contract this tracker has ever seen. Filed where a contract is filed,
        # because which underlying a contract is on cannot change.
        self._keys_by_underlying_side: dict[tuple[str, str], set[str]] = {}

    def observe_listing(self, listing) -> None:
        """One instrument listing -- either an underlying itself, or an option contract on one."""
        if listing.underlying_key is None:
            self._underlying_symbol_by_key[listing.instrument_key] = listing.trading_symbol
            for key, pending in list(self._pending_contracts.items()):
                if pending[0] == listing.instrument_key:
                    self._contracts[key] = (listing.trading_symbol, *pending[1:])
                    self._file_by_underlying(key, listing.trading_symbol, pending[1])
                    del self._pending_contracts[key]
            return
        if listing.instrument_type not in (CALL, PUT):
            return
        self._key_by_trading_symbol[listing.trading_symbol] = listing.instrument_key
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
            self._file_by_underlying(listing.instrument_key, symbol, listing.instrument_type)

    def _file_by_underlying(self, instrument_key: str, underlying_symbol: str, side: str) -> None:
        """Index this contract under its underlying and side.

        The whole cost of the ATM pick. `_atm_for` filtered every contract in
        `_contracts` to find the handful on one underlying, and
        `observe_option_listing` calls it twice for every listing that arrives --
        so once `broker-instrument-catalogue-reader` began restating the master
        continuously on 2026-09-04, `instrument-selector` spent 0.986 of a core
        scanning about 87,000 contracts twice per listing, roughly ten million
        comparisons a second. Which underlying a contract is on is decided when
        the contract is filed and cannot change, so it is decided there.
        """
        self._keys_by_underlying_side.setdefault((underlying_symbol, side), set()).add(
            instrument_key
        )

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

    def contract_by_symbol(self, trading_symbol: str, now_ms: int | None = None) -> AtmChoice | None:
        """The contract this exact trading_symbol names, not the nearest to ATM.

        Added 2026-09-08: a candidate that names a specific far-OTM contract
        (expiry-day-zero-to-hero-detector's whole thesis is a strike chosen
        *because* it is cheap and far from 0.5 delta) has no way to become a
        selectable instrument through `atm_call_for`/`atm_put_for` -- those
        deliberately return only the strike closest to 0.5 delta, so the
        contract the detector actually wants was never even a candidate.
        `instrument-selector` silently substituted the ATM pair for it
        instead, which is the opposite bet.

        None under the same two conditions `_atm_for` already refuses on: no
        delta observed yet for this contract, or its expiry has passed (or,
        given `now_ms`, has already happened) -- a stale symbol from a rolled
        contract is not resolved into an order.
        """
        key = self._key_by_trading_symbol.get(trading_symbol)
        if key is None or key not in self._latest_delta:
            return None
        record = self._contracts.get(key)
        if record is None or record[0] is None:
            return None
        _, side, name, strike, expiry_ms = record
        if now_ms is not None and (expiry_ms is None or expiry_ms <= now_ms):
            return None
        delta = self._latest_delta[key]
        return AtmChoice(
            instrument_key=key, trading_symbol=name, strike_price=strike,
            expiry_ms=expiry_ms, delta=delta,
            distance_from_atm=abs(delta - ATM_TARGET_DELTA[side]),
        )

    def contract_named(self, trading_symbol: str) -> tuple[str, str] | None:
        """(underlying_symbol, "CE" or "PE") for a contract, by the name it trades under.

        None when this is not a contract this tracker has seen, or when the
        listing arrived before the underlying it refers to and the underlying's
        own name is therefore still unknown. Both are the same answer to the
        caller -- "this cannot be resolved yet" -- and neither is a guess.
        """
        key = self._key_by_trading_symbol.get(trading_symbol)
        if key is None:
            return None
        record = self._contracts.get(key)
        if record is None or record[0] is None:
            return None
        return record[0], record[1]

    def atm_call_for(self, underlying_symbol: str, now_ms: int | None = None) -> AtmChoice | None:
        return self._atm_for(underlying_symbol, CALL, now_ms)

    def atm_put_for(self, underlying_symbol: str, now_ms: int | None = None) -> AtmChoice | None:
        return self._atm_for(underlying_symbol, PUT, now_ms)

    def _atm_for(self, underlying_symbol: str, side: str, now_ms: int | None) -> AtmChoice | None:
        # One underlying's ladder, from the index, rather than a filter over every
        # contract the tracker holds -- see `_file_by_underlying` for what that
        # cost when the catalogue started arriving continuously.
        candidates = [
            (key, self._contracts[key])
            for key in self._keys_by_underlying_side.get((underlying_symbol, side), ())
            if key in self._latest_delta and key in self._contracts
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
