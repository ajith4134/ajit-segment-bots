"""One past session's instruments, held the way the option segments would have held them.

The detector-edge measurement (docs/superpowers/plans/2026-09-16-detector-edge-across-sessions.md)
runs the detectors over many past sessions, and each needs what the live feed would
have carried that day: each underlying, and its contracts nearest the money on the
nearest expiry that had not yet passed.

**Past contracts come from Upstox's expired-instruments route.** The ordinary history
endpoint serves a listed option only since it listed and refuses an expired one, so
the current master cannot describe a past session's chain. The current master is
used only when that day's nearest expiry is still listed.

**Strikes are chosen by the session's open, not its close.** The live feed chooses by
where the market is, as it goes; choosing by where the day ended would pick contracts
with hindsight, and a detector measured on them would look better than it could have been.

**Which segment owns an underlying is asked, not typed** -- the same derived
membership the replay and the live spine use (`derived_membership_for`).

**Requests are spent a month at a time.** Upstox serves one-minute bars one month per
request, so the underlying and every contract are fetched for the calendar month that
holds the session and sliced to it. Every other session of that month is then free.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass

from operate.historical_prints import (
    expired_candles,
    expired_expiries,
    expired_option_contracts,
    historical_candles,
    prints_from_candles,
    upstox_access_token,
)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPTION_TYPES = ("CE", "PE")
EXPIRED_SOURCE = "upstox-expired"
LISTED_SOURCE = "upstox-history"


@dataclass(frozen=True)
class SessionInstrument:
    segment: str
    trading_symbol: str
    underlying: str
    option_type: str | None        # "CE", "PE", or None for the underlying itself
    strike: float | None
    expiry: str | None             # "YYYY-MM-DD"
    lot_size: float
    freeze_quantity: float
    instrument_key: str
    # (bar start ns, close), oldest first. A bar's close is known only when the
    # bar ends, so a reader replaying these must act at start + one interval.
    prints: tuple[tuple[int, float], ...]
    candles: tuple                          # the same bars, whole (BrokerCandle)
    source: str


def _session_bounds_ns(day: str) -> tuple[int, int]:
    """The session's open and close on `day`, from the operator's settings."""
    from operate.replay_a_captured_session import SettingsContext

    context = SettingsContext()

    def at(name: str) -> int:
        hour, minute = (int(part) for part in str(context.setting(name).value).split(":")[:2])
        moment = datetime.datetime.combine(
            datetime.date.fromisoformat(day), datetime.time(hour, minute), IST,
        )
        return int(moment.timestamp() * 1e9)

    return at("market_session_opens_at_ist"), at("market_session_closes_at_ist")


def _month_holding(day: str, not_after: datetime.date) -> tuple[str, str]:
    """The calendar month that holds `day`, cut at `not_after`."""
    date = datetime.date.fromisoformat(day)
    first = date.replace(day=1)
    last = date.replace(day=calendar.monthrange(date.year, date.month)[1])
    return first.isoformat(), min(last, not_after).isoformat()


def _in_session(candles, opens_ns: int, closes_ns: int) -> tuple:
    """The bars that start inside the session. A bar is stamped at its start, so the
    bar that starts at the close is after it."""
    return tuple(
        candle for candle in candles
        if candle.close and opens_ns <= candle.bar_time_ms * 1_000_000 < closes_ns
    )


def _segment_owning(underlying: str, membership: dict, segments) -> str | None:
    for segment in segments:
        if underlying in membership.get(segment, ()):
            return segment
    return None


def _underlying_row(master: dict, underlying: str) -> dict | None:
    """The one listing the options are written on -- not one per exchange that lists
    the name (the 2026-09-16 fix in broker-symbol-universe-bridge)."""
    keys = {
        row["underlying_key"] for row in master.values()
        if row.get("underlying_symbol") == underlying
        and row.get("instrument_type") in OPTION_TYPES
        and row.get("underlying_key")
    }
    for key in sorted(keys):
        if key in master:
            return master[key]
    return None


def _chain_for(master: dict, underlying_key: str, underlying: str, day: str, token: str):
    """(expiry, rows, source) for the nearest expiry on or after `day`."""
    expired = [expiry for expiry in expired_expiries(underlying_key, token) if expiry >= day]
    listed = sorted({
        datetime.datetime.fromtimestamp(row["expiry"] / 1000, IST).date().isoformat()
        for row in master.values()
        if row.get("underlying_symbol") == underlying
        and row.get("instrument_type") in OPTION_TYPES and row.get("expiry")
    })
    listed = [expiry for expiry in listed if expiry >= day]
    candidates = sorted(set(expired[:1]) | set(listed[:1]))
    if not candidates:
        return None, (), ""
    expiry = candidates[0]
    if expiry in expired:
        return expiry, expired_option_contracts(underlying_key, expiry, token), EXPIRED_SOURCE
    rows = tuple(
        row for row in master.values()
        if row.get("underlying_symbol") == underlying
        and row.get("instrument_type") in OPTION_TYPES and row.get("expiry")
        and datetime.datetime.fromtimestamp(row["expiry"] / 1000, IST).date().isoformat() == expiry
    )
    return expiry, rows, LISTED_SOURCE


def past_session_instruments(
    day: str, underlyings: tuple[str, ...], contracts_per_underlying: int, minimum_prints: int,
) -> tuple[SessionInstrument, ...]:
    """Each named underlying and its contracts nearest the session's open, for `day`.

    An underlying or contract with fewer than `minimum_prints` bars that session is
    left out: a contract nobody traded is not one a detector could have watched.
    """
    from operate.replay_a_captured_session import (
        SettingsContext, derived_membership_for, instruments_by_key,
    )
    from runtime.segment_settings import built_segments

    token = upstox_access_token()
    master = instruments_by_key()
    membership = derived_membership_for(master)
    segments = tuple(built_segments(SettingsContext()))
    opens_ns, closes_ns = _session_bounds_ns(day)
    yesterday = datetime.datetime.now(IST).date() - datetime.timedelta(days=1)
    held: list[SessionInstrument] = []

    for underlying in underlyings:
        segment = _segment_owning(underlying, membership, segments)
        row = _underlying_row(master, underlying)
        if segment is None or row is None:
            continue
        month_from, month_to = _month_holding(day, yesterday)
        spot_bars = _in_session(
            historical_candles(row["instrument_key"], month_from, month_to, token),
            opens_ns, closes_ns,
        )
        spot = tuple(prints_from_candles(spot_bars))
        if len(spot) < minimum_prints:
            continue
        # An index carries no lot, and an option segment never buys its underlying,
        # so an absent lot reads as zero -- not a size anything could be traded at.
        held.append(SessionInstrument(
            segment=segment, trading_symbol=underlying, underlying=underlying,
            option_type=None, strike=None, expiry=None,
            lot_size=float(row.get("lot_size") or 0), freeze_quantity=float(row.get("freeze_quantity") or 0),
            instrument_key=row["instrument_key"], prints=spot, candles=spot_bars,
            source=LISTED_SOURCE,
        ))

        expiry, chain, source = _chain_for(master, row["instrument_key"], underlying, day, token)
        if not chain:
            continue
        opening = spot[0][1]
        by_strike: dict[float, dict[str, dict]] = {}
        for contract in chain:
            by_strike.setdefault(float(contract["strike_price"]), {})[contract["instrument_type"]] = contract
        chosen = 0
        for strike in sorted(by_strike, key=lambda s: abs(s - opening)):
            if chosen >= contracts_per_underlying:
                break
            for option_type in OPTION_TYPES:
                contract = by_strike[strike].get(option_type)
                if contract is None or chosen >= contracts_per_underlying:
                    continue
                expiry_date = datetime.date.fromisoformat(expiry)
                contract_from, contract_to = _month_holding(day, min(expiry_date, yesterday))
                fetch = expired_candles if source == EXPIRED_SOURCE else historical_candles
                bars = _in_session(
                    fetch(contract["instrument_key"], contract_from, contract_to, token),
                    opens_ns, closes_ns,
                )
                prints = tuple(prints_from_candles(bars))
                if len(prints) < minimum_prints:
                    continue
                held.append(SessionInstrument(
                    segment=segment, trading_symbol=contract["trading_symbol"],
                    underlying=underlying, option_type=option_type, strike=strike,
                    expiry=expiry, lot_size=float(contract["lot_size"]),
                    freeze_quantity=float(contract.get("freeze_quantity") or 0),
                    instrument_key=contract["instrument_key"], prints=prints, candles=bars,
                    source=source,
                ))
                chosen += 1
    return tuple(held)


__all__ = ["SessionInstrument", "past_session_instruments"]
