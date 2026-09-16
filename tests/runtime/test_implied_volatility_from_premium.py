"""An implied volatility inverted from a premium agrees with the one Upstox states.

Real data only (RL-063): every contract on the captured tape for one session, its
own premium and its underlying's price at the moment Upstox published a greeks
record, inverted and compared with the `implied_volatility` that record carries.
"""

from __future__ import annotations

import bisect
import datetime
import json
import pathlib
import statistics

import pytest

from runtime.implied_volatility_from_premium import implied_volatility, option_price
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_payload, read_tape_index

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
DAY = "2026-09-15"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _number(name: str) -> float:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return float(document.read_value(name))


def _series(stem: pathlib.Path, field: str):
    """(broker times, values) for one stream of one day, oldest first."""
    index = stem.with_name(stem.name + ".index")
    blob = stem.with_name(stem.name + ".blob")
    if not index.exists():
        return [], []
    times, values = [], []
    for record in read_tape_index(index):
        try:
            payload = json.loads(read_payload(blob, record))
        except ValueError:
            continue
        if payload.get(field):
            times.append(int(record[1]))
            values.append(float(payload[field]))
    return times, values


def _seconds_to_expiry_close(at_ns: int, expiry_text: str, close_text: str) -> float:
    hour, minute = (int(part) for part in close_text.split(":")[:2])
    expiry = datetime.datetime.strptime(expiry_text, "%d %b %y").replace(
        hour=hour, minute=minute, tzinfo=IST)
    return expiry.timestamp() - at_ns / 1e9


def _paired_disagreements(limit: int) -> list[float]:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    close_text = str(document.read_value("market_session_closes_at_ist"))
    seconds_per_year = _number("option_delta_seconds_per_year")
    rate = _number("implied_volatility_annual_carry_rate")
    tolerance = _number("implied_volatility_search_tolerance")
    ceiling = _number("implied_volatility_search_ceiling")
    spot_cache: dict[str, tuple] = {}
    errors: list[float] = []
    for directory in sorted(TAPE.iterdir()):
        parts = directory.name.split()
        if len(parts) < 4 or parts[2] not in ("CE", "PE"):
            continue
        underlying = parts[0]
        if underlying not in spot_cache:
            spot_cache[underlying] = _series(TAPE / underlying / DAY, "last_traded_price")
        spot_times, spots = spot_cache[underlying]
        greek_times, stated = _series(directory / f"{DAY}.option_greeks", "implied_volatility")
        trade_times, premiums = _series(directory / DAY, "last_traded_price")
        if not spot_times or not greek_times or not trade_times:
            continue
        step = max(1, len(stated) // 5)
        for at, iv in list(zip(greek_times, stated))[::step]:
            p = bisect.bisect_right(trade_times, at) - 1
            s = bisect.bisect_right(spot_times, at) - 1
            if p < 0 or s < 0:
                continue
            ours = implied_volatility(
                premiums[p], spots[s], float(parts[1]), parts[2],
                _seconds_to_expiry_close(at, " ".join(parts[3:]), close_text),
                seconds_per_year, rate, tolerance, ceiling,
            )
            if ours is not None:
                errors.append(abs(ours - iv))
        if len(errors) >= limit:
            break
    return errors


def test_inverted_volatility_matches_what_upstox_states():
    errors = _paired_disagreements(limit=500)
    if len(errors) < 50:
        pytest.skip(f"only {len(errors)} greeks records could be paired on {DAY}")
    assert statistics.median(errors) < _number("implied_volatility_agreement_tolerance")


def test_the_inverted_volatility_brackets_the_premium():
    """Round trip on real prices: the premium lies between the prices one search
    tolerance either side of the volatility found -- which is exactly what a
    bisection to that tolerance promises, and nothing looser."""
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    close_text = str(document.read_value("market_session_closes_at_ist"))
    seconds_per_year = _number("option_delta_seconds_per_year")
    rate = _number("implied_volatility_annual_carry_rate")
    tolerance = _number("implied_volatility_search_tolerance")
    ceiling = _number("implied_volatility_search_ceiling")
    directory = next(
        (d for d in sorted(TAPE.iterdir())
         if d.name.startswith("NIFTY ") and (d / f"{DAY}.index").exists()),
        None,
    )
    if directory is None:
        pytest.skip(f"no NIFTY contract captured on {DAY}")
    parts = directory.name.split()
    strike, kind = float(parts[1]), parts[2]
    spot_times, spots = _series(TAPE / "NIFTY" / DAY, "last_traded_price")
    trade_times, premiums = _series(directory / DAY, "last_traded_price")
    checked = 0
    for at, premium in list(zip(trade_times, premiums))[:: max(1, len(premiums) // 50)]:
        s = bisect.bisect_right(spot_times, at) - 1
        if s < 0:
            continue
        seconds = _seconds_to_expiry_close(at, " ".join(parts[3:]), close_text)
        found = implied_volatility(premium, spots[s], strike, kind,
                                   seconds, seconds_per_year, rate, tolerance, ceiling)
        if found is None:
            continue
        below = option_price(spots[s], strike, kind, max(found - tolerance, tolerance),
                             seconds, seconds_per_year, rate)
        above = option_price(spots[s], strike, kind, found + tolerance,
                             seconds, seconds_per_year, rate)
        assert below <= premium <= above
        checked += 1
    if not checked:
        pytest.skip(f"no invertible premium for {directory.name} on {DAY}")
