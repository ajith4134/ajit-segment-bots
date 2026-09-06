"""Free historical prices for every NSE option and future, from NSE itself.

**The two options segments are Phase A, and no free source carried them.** Yahoo
has no NSE option chain, and Upstox -- the only intraday source this project has
for a contract -- answers HTTP 429 once its daily quota is spent. So an options
replay could only run while that quota lasted, which is not a source anybody can
build on.

NSE publishes its own end-of-day derivatives file, free, with no account, no key
and no registration. Measured 2026-09-06 for the session of 2026-09-04:

    32,515 rows in one file
       26,872 stock options (STO)      4,996 index options (IDO)
          629 stock futures (STF)         18 index futures (IDF)

with `OpnPric`, `HghPric`, `LwPric`, `ClsPric`, `TtlTradgVol` and `OpnIntrst`
per contract. That is **complete** coverage of both options segments -- every
strike and every expiry, not the 2,000 the live feed can subscribe to.

Depth, measured the same day by fetching each file:

    2026-09-04  32,515 rows      2025-09-05  29,306 rows
    2024-09-06  30,995 rows      2023-09-04  no file

so roughly two years, which is far more history than this project has ever had
for an option.

**It is daily, and that is the trade this makes.** One OHLC per contract per
session, against Upstox's one-minute bars. A replay walking four prices a day
cannot test an intraday stop the way a minute series can, and this never pretends
otherwise -- it is breadth and history where Upstox is depth. `ClsPric` is also
NSE's own **closing price**, computed from the last half hour rather than being
the final print: measured against this project's captured tape across 40
contracts, the median difference to the tape's last print is 4.5%, which is what
those two numbers genuinely are rather than a disagreement about the market.

The earlier note that `nsearchives.nseindia.com` was blocked by bot protection
was about a different path: the participant and derivatives files answer a warmed
`curl_cffi` session with 200, and it was the *filename* that was wrong. The
legacy `content/historical/DERIVATIVES/...` names are gone; the UDiFF ones under
`content/fo/` are what NSE serves now.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import zipfile
from dataclasses import dataclass

NSE_HOME = "https://www.nseindia.com"
BHAVCOPY_HOST = "https://nsearchives.nseindia.com/content/fo"
# Written next to the fetched file, because a day's settled derivatives file
# never changes once published and refetching a megabyte proves nothing.
BHAVCOPY_CACHE = pathlib.Path.home() / ".local/share/ajit-segment-bots/history/nse-fo-bhavcopy"

INDEX_OPTION = "IDO"
STOCK_OPTION = "STO"
OPTION_KINDS = (INDEX_OPTION, STOCK_OPTION)


@dataclass(frozen=True)
class ContractDay:
    """One contract's whole session, as NSE settled it."""

    underlying: str
    expiry: str
    strike: float
    option_type: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float

    @property
    def traded(self) -> bool:
        """Whether anybody actually traded it.

        A contract with no volume still carries a close -- NSE settles every
        listed strike -- and replaying that close as a price would be replaying
        a market that did not exist.
        """
        return self.volume > 0


def bhavcopy_url(day: str) -> str:
    """NSE's own UDiFF name for one session's derivatives file. `day` is YYYYMMDD."""
    return f"{BHAVCOPY_HOST}/BhavCopy_NSE_FO_0_0_0_{day}_F_0000.csv.zip"


def cache_path_for(day: str) -> pathlib.Path:
    return BHAVCOPY_CACHE / f"{day}.csv"


def open_browser_session():
    """A warmed session, as `runtime/nse_public_data.py` already establishes.

    NSE refuses a call from a session that has not fetched the homepage: the
    homepage sets the cookie every later call is checked against.
    """
    from curl_cffi import requests

    session = requests.Session(impersonate="chrome")
    session.get(NSE_HOME, timeout=20)
    return session


def fetch_bhavcopy(day: str, session=None, timeout_seconds: float = 40.0) -> str:
    """One session's derivatives file as CSV text, cached on disk.

    A non-200 raises rather than returning empty. An error page parsed as a
    bhavcopy is an empty bhavcopy, and an empty one reads as "nothing traded
    that day" -- the failure that looks exactly like the good case, which is the
    rule `NsePublicData` already states for the same host.
    """
    path = cache_path_for(day)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        pass

    session = session or open_browser_session()
    response = session.get(bhavcopy_url(day), timeout=timeout_seconds)
    if response.status_code != 200:
        raise RuntimeError(
            f"NSE has no derivatives file for {day}: HTTP {response.status_code}. "
            f"A holiday and a wrong filename are different facts, and this says which "
            f"was asked for: {bhavcopy_url(day)}"
        )
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    text = archive.read(archive.namelist()[0]).decode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        being_written = path.with_suffix(".writing")
        being_written.write_text(text, encoding="utf-8")
        being_written.replace(path)
    except OSError:
        pass  # a cache that cannot be written is a slower fetch, never a wrong one
    return text


def contracts_in(text: str) -> list[ContractDay]:
    """Every option contract in one bhavcopy, futures excluded.

    Futures are dropped here rather than filtered by the caller because this
    project's options segments are what needs them, and a caller that forgot
    would size an option position against a future's price.
    """
    contracts = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("FinInstrmTp") not in OPTION_KINDS:
            continue
        option_type = (row.get("OptnTp") or "").strip()
        if option_type not in ("CE", "PE"):
            continue
        try:
            contracts.append(
                ContractDay(
                    underlying=(row.get("TckrSymb") or "").strip(),
                    expiry=(row.get("XpryDt") or "").strip(),
                    strike=float(row["StrkPric"]),
                    option_type=option_type,
                    open=float(row["OpnPric"]),
                    high=float(row["HghPric"]),
                    low=float(row["LwPric"]),
                    close=float(row["ClsPric"]),
                    volume=float(row.get("TtlTradgVol") or 0),
                    open_interest=float(row.get("OpnIntrst") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            # A row this reader cannot parse is skipped and not guessed at. NSE
            # has changed this file's shape before, and a fabricated strike is
            # worse than a contract nobody replayed.
            continue
    return contracts


def chain_of(contracts: list[ContractDay], underlying: str,
             traded_only: bool = True) -> list[ContractDay]:
    """One underlying's contracts, optionally only the ones that traded."""
    return [
        contract for contract in contracts
        if contract.underlying == underlying and (contract.traded or not traded_only)
    ]


def nearest_the_money(chain: list[ContractDay], spot: float,
                      expiry: str | None = None) -> list[ContractDay]:
    """That chain's contracts, closest strike to `spot` first.

    Nearest expiry when none is named, because that is where the volume is.
    """
    if not chain:
        return []
    wanted = expiry or min(contract.expiry for contract in chain)
    at_expiry = [contract for contract in chain if contract.expiry == wanted]
    return sorted(at_expiry, key=lambda contract: abs(contract.strike - spot))


__all__ = [
    "BHAVCOPY_CACHE",
    "ContractDay",
    "INDEX_OPTION",
    "OPTION_KINDS",
    "STOCK_OPTION",
    "bhavcopy_url",
    "cache_path_for",
    "chain_of",
    "contracts_in",
    "fetch_bhavcopy",
    "nearest_the_money",
    "open_browser_session",
]
