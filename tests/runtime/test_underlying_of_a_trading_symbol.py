"""The underlying of a trading symbol, against the real instrument master (RL-063).

`volatility-gap-detector` derived this with `symbol.rstrip("USDT")`, which is not
"remove this suffix" -- it strips every trailing character in the set. These run
the rule over every NSE F&O underlying Upstox actually lists, because the defect
it replaces is invisible per-symbol: a mangled key does not raise, it just fails
to match, and the part's counters then read exactly as they do when a symbol is
quiet.
"""

from __future__ import annotations

import gzip
import json
import pathlib

import pytest

from runtime.underlying_of_a_trading_symbol import underlying_of_a_trading_symbol

MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"


@pytest.fixture(scope="module")
def real_master():
    assert MASTER.exists(), (
        f"{MASTER} is missing. This runs against the broker's real instrument "
        f"master (RL-063); there is no hand-written fallback."
    )
    return json.load(gzip.open(MASTER))


@pytest.fixture(scope="module")
def fno_underlyings(real_master):
    names = sorted({
        row["underlying_symbol"]
        for row in real_master
        if row.get("instrument_type") in ("CE", "PE")
        and row.get("underlying_type") == "EQUITY"
        and row.get("underlying_symbol")
    })
    assert len(names) > 150, "the F&O stock list is 210 names, not a handful"
    return names


@pytest.fixture(scope="module")
def option_trading_symbols(real_master):
    rows = [
        row for row in real_master
        if row.get("instrument_type") in ("CE", "PE")
        and row.get("segment") in ("NSE_FO", "BSE_FO")
        and row.get("trading_symbol") and row.get("underlying_symbol")
    ]
    assert len(rows) > 10_000
    return rows


def test_every_fno_underlying_survives_unchanged(fno_underlyings):
    """A share is its own underlying. The old rule mangled 39 of these."""
    mangled = [name for name in fno_underlyings if underlying_of_a_trading_symbol(name) != name]
    assert mangled == [], f"{len(mangled)} underlying(s) were altered: {mangled[:10]}"


def test_the_rule_this_replaces_really_did_mangle_them(fno_underlyings):
    """The regression guard. Without it nothing records that this was ever wrong.

    Kept as an assertion rather than a comment because `rstrip` looks like a
    suffix strip to every reader, which is why it survived in the code.
    """
    mangled = [name for name in fno_underlyings if name.rstrip("USDT") != name]
    assert len(mangled) >= 30, (
        "rstrip('USDT') strips any trailing U, S, D or T; if this no longer "
        "mangles NSE names the master has changed shape, not the rule"
    )
    assert "LT" in mangled and "HAVELLS" in mangled


def test_an_option_resolves_to_the_share_it_is_written_on(option_trading_symbols):
    """The half the settlement-suffix-only copies got wrong.

    "HINDUNILVR 1980 PE 29 SEP 26" ends in no settlement currency, so that rule
    returned the whole contract name and the option never resolved at all.
    """
    checked = 0
    for row in option_trading_symbols[:5000]:
        derived = underlying_of_a_trading_symbol(row["trading_symbol"])
        assert derived == row["underlying_symbol"], (
            f"{row['trading_symbol']!r} resolved to {derived!r}, and the master "
            f"states {row['underlying_symbol']!r}"
        )
        checked += 1
    assert checked > 1000


def test_an_index_option_resolves_to_its_index(real_master):
    rows = [
        row for row in real_master
        if row.get("instrument_type") in ("CE", "PE")
        and row.get("underlying_type") == "INDEX"
        and row.get("trading_symbol") and row.get("underlying_symbol")
    ]
    assert rows
    for row in rows[:2000]:
        assert underlying_of_a_trading_symbol(row["trading_symbol"]) == row["underlying_symbol"]


def test_a_settlement_suffixed_perpetual_still_resolves():
    """The crypto shape the rule was originally written for stays right.

    Convert or replace, never merely delete: a venue whose symbols carry a
    settlement suffix is a real shape, and this keeps working for it.
    """
    assert underlying_of_a_trading_symbol("BTCUSDT", settlement="USDT") == "BTC"
    assert underlying_of_a_trading_symbol("ETHUSDC", settlement="USDC") == "ETH"


def test_the_suffix_is_removed_by_slicing_not_by_stripping_characters():
    """`rstrip` would eat a run of characters; a suffix removal takes one suffix."""
    # Every letter here is in the set {U,S,D,T}, so rstrip would return "".
    assert "TUSDUSDT".rstrip("USDT") == ""
    assert underlying_of_a_trading_symbol("TUSDUSDT", settlement="USDT") == "TUSD"


def test_a_symbol_with_no_suffix_and_no_space_is_its_own_underlying():
    assert underlying_of_a_trading_symbol("RELIANCE") == "RELIANCE"
    assert underlying_of_a_trading_symbol("NIFTY", settlement="USDT") == "NIFTY"


def test_an_empty_symbol_is_returned_rather_than_indexed_into():
    assert underlying_of_a_trading_symbol("") == ""
