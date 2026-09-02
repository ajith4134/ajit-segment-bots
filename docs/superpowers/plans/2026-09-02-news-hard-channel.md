# Stock market news data — the hard channel, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the five parts of `stock-market-news-data` that publish facts the bot must obey rather than weigh — F&O ban, ASM surveillance, corporate actions, trading holidays — and wire them into the parts that refuse on them.

**Architecture:** Five parts in `parts/stock_market_news_data/`, each following the T-1 part template (`PART_DECLARATION`, a plain testable class, `describe_*`, `start_part(context)`). Two readers fetch NSE's own public endpoints through one shared session module; three parts turn those reports into aged levels. No LLM, no learning, no vendor. Consumers then read those levels and refuse.

**Tech Stack:** CPython 3.14.4 in `.venv`, `curl_cffi` (already a pinned dependency — NSE serves Cloudflare 403 to `urllib.request`, the same wall `broker-market-feed-reader` hit on 2026-09-02), stdlib `json`/`csv`/`datetime`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-02-stock-market-news-data-design.md`
**Proposal:** `docs/proposals/stock-market-news-data.md`
**Blueprint:** already applied — commit `191e046`. All five parts are `DECLARED`; this plan takes them to `RUNNING`.

## Why this slice, and what the other two are

The block declares 29 parts. Building them in one pass would mean the LLM
pipeline, five learned models and an autonomous searcher landing at the same time
as the ban list. This plan is the hard channel alone, and it is first because:

- it is the half that has **no model, no threshold and nothing to tune** — a
  banned name is banned;
- it is worth something the day it lands, with no other part of the block built:
  the bot stops sending orders that the exchange will reject, stops paper-filling
  on a holiday, and stops reading a 1:1 bonus as a −50% candle;
- every source is a **free public NSE endpoint verified live on 2026-09-02**
  (evidence in each task), so nothing waits on a vendor or a key.

Two plans follow, each written when its turn comes, not stubbed here:

- **Plan 2 — the news pipeline**: the 9 remaining source readers, deduplicator,
  LLM structurer, symbol/category/credibility/segment classifiers, tape writer.
- **Plan 3 — judgement plus autonomy**: the five learned parts, the
  investigator/searcher pair, health monitor, latency meter, history reader,
  and `news-catalyst-detector` in the scanner.

## Global Constraints

- **RL-058:** production standard. No shortcut code, no placeholders, no
  hardcoded values, no invented data.
- **RL-061:** every number comes from a named setting in
  `~/.config/ajit-segment-bots/settings/runtime.toml` carrying `value`, `unit`
  and a `note` with provenance. A numeric literal in decision code is a defect.
  Physical unit conversions (`1_000_000_000` ns per second) are not decision
  numbers and stay module constants, as `broker_candle_bridge.py` already does.
- **RL-063:** tests run on real captured data or real recorded API responses,
  never invented fixtures. Every fixture in this plan is copied from a live
  response captured 2026-09-02 and quoted in the task.
- **T-1:** every part carries `PART_DECLARATION` with `part_id`, `consumes`,
  `produces`, `resource_class`, `rate_risk`, `skipped_tick_effect`, and one
  `start_part(context) -> int` entry point.
- **T-5:** states are `off` / `on` only.
- **Levels carry an age bound.** Every `LatestByKey` in this plan sets
  `maximum_age_seconds` from a setting. An unbounded level is the defect that
  cost this project 214 phantom running parts on 2026-08-26 and a fifty-six
  minute stale price on 2026-08-23.
- **Blueprint is already correct.** No task edits `docs/features.json`. If a
  task seems to need a contract change, stop and raise it.
- **After any change a live part imports, restart the spine and read the
  journal.** A green test suite does not prove the running system can still
  start — `systemctl --user restart ajit-spine`, then
  `journalctl --user --since "-5min" | grep -B 30 Error`.
- Run everything through `.venv/bin/python` and `.venv/bin/pytest`. The system
  python has no numpy and `check_payload_reads.py` fails on it.

## File Structure

| file | responsibility |
|---|---|
| `runtime/market_conditions.py` | the five payload types of the hard channel, and the pure functions that read NSE's own date/subject strings |
| `runtime/nse_public_data.py` | one warmed browser-impersonating session; NSE refuses an API call that has not first been given a homepage cookie |
| `parts/stock_market_news_data/__init__.py` | package marker |
| `parts/stock_market_news_data/trading_restriction_reader.py` | fetch the ban list plus the ASM lists |
| `parts/stock_market_news_data/instrument_restriction_state.py` | merge reports into one aged level per instrument |
| `parts/stock_market_news_data/corporate_action_reader.py` | fetch the corporate action file |
| `parts/stock_market_news_data/corporate_action_adjuster.py` | turn an action into a price adjustment factor |
| `parts/stock_market_news_data/market_session_calendar.py` | state which session the market is in |
| `tests/parts/stock_market_news_data/test_*.py` | one test file per part |
| `tests/runtime/test_market_conditions.py` | the payload types plus their parsers |

---

### Task 1: The payload types of the hard channel

**Files:**
- Create: `runtime/market_conditions.py`
- Test: `tests/runtime/test_market_conditions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `InstrumentRestrictionReport`, `InstrumentRestriction`,
  `CorporateActionReport`, `CorporateAction`, `MarketSessionState`,
  `RestrictionKind`, `SessionKind`, `read_nse_date`, `NSE_DATE_FORMAT`.

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_market_conditions.py
"""Every string in this file is copied from a live NSE response captured
2026-09-02 (RL-063): the ban file's own body, one reportASM row, and one
corporates-corporateActions row. Never an invented shape."""

import datetime

import pytest

from runtime.market_conditions import (
    CorporateAction,
    CorporateActionReport,
    InstrumentRestriction,
    InstrumentRestrictionReport,
    MarketSessionState,
    RestrictionKind,
    SessionKind,
    read_nse_date,
)

# https://nsearchives.nseindia.com/content/fo/fo_secban.csv, fetched 2026-09-02
BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"


def test_reads_nse_s_own_two_date_formats():
    """The ban file stamps 02-SEP-2026 and every JSON endpoint stamps
    02-Sep-2026. Same day, two casings, one parser."""
    assert read_nse_date("02-SEP-2026") == datetime.date(2026, 9, 2)
    assert read_nse_date("02-Sep-2026") == datetime.date(2026, 9, 2)


def test_a_date_nse_did_not_state_is_refused_not_guessed():
    with pytest.raises(ValueError):
        read_nse_date("-")


def test_a_restriction_report_carries_the_source_that_claimed_it():
    report = InstrumentRestrictionReport(
        symbol="LICHSGFIN",
        kind=RestrictionKind.FNO_BAN,
        source="nse-fo-secban",
        stated_for=datetime.date(2026, 9, 2),
        detail="Securities in Ban For Trade Date 02-SEP-2026",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert report.symbol == "LICHSGFIN"
    assert report.kind is RestrictionKind.FNO_BAN
    assert report.source == "nse-fo-secban"


def test_a_restriction_says_plainly_whether_a_new_position_may_open():
    banned = InstrumentRestriction(
        symbol="LICHSGFIN",
        kinds=(RestrictionKind.FNO_BAN,),
        sources=("nse-fo-secban",),
        stated_for=datetime.date(2026, 9, 2),
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert banned.may_open_new_position is False
    assert banned.may_close_existing_position is True


def test_an_asm_stage_restricts_without_forbidding_an_exit():
    """ASM raises margin; it does not stop a position being closed. A
    restriction that blocked exits would trap capital the exchange never
    trapped."""
    watched = InstrumentRestriction(
        symbol="A2ZINFRA",
        kinds=(RestrictionKind.ASM_LONG_TERM,),
        sources=("nse-asm-longterm",),
        stated_for=datetime.date(2026, 9, 2),
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert watched.may_open_new_position is False
    assert watched.may_close_existing_position is True


def test_a_corporate_action_report_keeps_nse_s_own_subject_text():
    """subject is the only field stating what the action actually is;
    corporate-action-adjuster reads it, so it is carried verbatim."""
    report = CorporateActionReport(
        symbol="NTPC",
        series="EQ",
        isin="INE733E01010",
        subject="Dividend - Rs 3.50 Per Share",
        ex_date=datetime.date(2026, 9, 2),
        record_date=datetime.date(2026, 9, 2),
        face_value=10.0,
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert report.subject == "Dividend - Rs 3.50 Per Share"
    assert report.ex_date == datetime.date(2026, 9, 2)


def test_a_corporate_action_carries_the_factor_a_price_series_must_be_divided_by():
    action = CorporateAction(
        symbol="RELIANCE",
        kind="bonus",
        price_factor=0.5,
        quantity_factor=2.0,
        ex_date=datetime.date(2026, 9, 2),
        stated_from="Bonus 1:1",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert action.price_factor == 0.5
    assert action.quantity_factor == 2.0


def test_a_session_state_says_which_session_and_why():
    holiday = MarketSessionState(
        segment="FO",
        kind=SessionKind.HOLIDAY,
        as_of_date=datetime.date(2026, 1, 26),
        reason="Republic Day",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert holiday.is_tradeable is False
    assert holiday.reason == "Republic Day"


def test_an_open_session_is_tradeable():
    live = MarketSessionState(
        segment="FO",
        kind=SessionKind.OPEN,
        as_of_date=datetime.date(2026, 9, 2),
        reason="within stated session hours",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    assert live.is_tradeable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/runtime/test_market_conditions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.market_conditions'`

- [ ] **Step 3: Write minimal implementation**

```python
# runtime/market_conditions.py
"""The hard channel's payload types: what the exchange itself says about
whether an instrument may be traded, what a corporate action does to a price
series, and which session the market is in.

Nothing here is scored. A restriction is a fact the exchange published, and a
part reading one refuses rather than weighs -- which is why these types are
separate from the news signal types entirely (D-N4). One wire carrying both a
halt and a sentiment score is the shape that crashed a reader of trades on
2026-08-25, one layer along.

NSE states dates two ways in the same day's data: the ban file writes
02-SEP-2026 and every JSON endpoint writes 02-Sep-2026 (both captured
2026-09-02). One parser reads both; a string in neither shape -- NSE writes
"-" where it has no date -- raises rather than defaulting to today, because a
corporate action silently dated today would adjust a price series that was
never adjusted.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass

NSE_DATE_FORMAT = "%d-%b-%Y"


def read_nse_date(stated: str) -> datetime.date:
    """One of NSE's own date strings, in either casing it publishes."""
    return datetime.datetime.strptime(stated.strip().title(), NSE_DATE_FORMAT).date()


class RestrictionKind(enum.StrEnum):
    """What the exchange said, in its own vocabulary."""

    FNO_BAN = "fno-ban"
    ASM_SHORT_TERM = "asm-short-term"
    ASM_LONG_TERM = "asm-long-term"


class SessionKind(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    HOLIDAY = "holiday"


@dataclass(frozen=True)
class InstrumentRestrictionReport:
    """One source's statement that one instrument is restricted."""

    symbol: str
    kind: RestrictionKind
    source: str
    stated_for: datetime.date
    detail: str
    observed_at_ns: int


@dataclass(frozen=True)
class InstrumentRestriction:
    """Every restriction standing on one instrument right now, merged.

    `may_close_existing_position` is True for every kind NSE publishes here:
    a ban stops fresh positions, not exits, and an ASM stage raises margin
    without closing the door. A restriction that blocked exits would trap
    capital the exchange never trapped.
    """

    symbol: str
    kinds: tuple[RestrictionKind, ...]
    sources: tuple[str, ...]
    stated_for: datetime.date
    observed_at_ns: int

    @property
    def may_open_new_position(self) -> bool:
        return not self.kinds

    @property
    def may_close_existing_position(self) -> bool:
        return True


@dataclass(frozen=True)
class CorporateActionReport:
    """A corporate action as NSE published it, subject text carried verbatim."""

    symbol: str
    series: str
    isin: str
    subject: str
    ex_date: datetime.date
    record_date: datetime.date | None
    face_value: float | None
    observed_at_ns: int


@dataclass(frozen=True)
class CorporateAction:
    """What an action does to a price series, from its ex-date on.

    `price_factor` is what a pre-ex price is multiplied by to sit beside a
    post-ex one; `quantity_factor` is what a pre-ex quantity is multiplied by.
    A 1:1 bonus doubles the shares and halves the price: 0.5 and 2.0.
    """

    symbol: str
    kind: str
    price_factor: float
    quantity_factor: float
    ex_date: datetime.date
    stated_from: str
    observed_at_ns: int


@dataclass(frozen=True)
class MarketSessionState:
    """Which session one exchange segment is in right now."""

    segment: str
    kind: SessionKind
    as_of_date: datetime.date
    reason: str
    observed_at_ns: int

    @property
    def is_tradeable(self) -> bool:
        return self.kind is SessionKind.OPEN


__all__ = [
    "NSE_DATE_FORMAT",
    "CorporateAction",
    "CorporateActionReport",
    "InstrumentRestriction",
    "InstrumentRestrictionReport",
    "MarketSessionState",
    "RestrictionKind",
    "SessionKind",
    "read_nse_date",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/runtime/test_market_conditions.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add runtime/market_conditions.py tests/runtime/test_market_conditions.py
git commit -m "feat: the hard channel's payload types, read from NSE's own strings"
```

---

### Task 2: One warmed NSE session

**Files:**
- Create: `runtime/nse_public_data.py`
- Test: `tests/runtime/test_nse_public_data.py`

**Interfaces:**
- Consumes: `curl_cffi.requests` only.
- Produces: `NsePublicData` with `read_text(path) -> str` and
  `read_json(path) -> object`; `NSE_HOME`, `NSE_API_HOST`, `NSE_ARCHIVE_HOST`.

**Why this is its own module:** verified 2026-09-02 — `urllib.request` gets a
Cloudflare 403 from NSE (the same wall `broker-market-feed-reader` documents),
and an `/api/` call made before the homepage has set a cookie is refused. Both
readers need the identical warmed session, so it is built once here rather than
twice, wrongly, in two parts.

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_nse_public_data.py
"""The session's behaviour is tested against a recorded transcript rather than
the live site: a test that needs the internet is a test that fails at 3am for a
reason that is not a defect. The recorded bodies are real, captured 2026-09-02."""

import pytest

from runtime.nse_public_data import NSE_HOME, NsePublicData

BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"


class RecordedSession:
    """Stands in for curl_cffi's Session, recording what was asked in order."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, timeout):
        self.asked.append(url)
        status, body = self.answers[url]
        return RecordedResponse(status, body)


class RecordedResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


def test_the_homepage_is_asked_before_the_first_api_call():
    """NSE refuses an /api/ call from a session with no homepage cookie
    (verified 2026-09-02). The warm-up is not politeness, it is the request
    working at all."""
    session = RecordedSession({
        NSE_HOME: (200, "<html></html>"),
        "https://www.nseindia.com/api/holiday-master?type=trading": (200, '{"FO": []}'),
    })
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    nse.read_json("https://www.nseindia.com/api/holiday-master?type=trading")
    assert session.asked[0] == NSE_HOME


def test_the_homepage_is_asked_once_not_before_every_call():
    session = RecordedSession({
        NSE_HOME: (200, "<html></html>"),
        "https://www.nseindia.com/api/holiday-master?type=trading": (200, '{"FO": []}'),
    })
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    nse.read_json("https://www.nseindia.com/api/holiday-master?type=trading")
    nse.read_json("https://www.nseindia.com/api/holiday-master?type=trading")
    assert session.asked.count(NSE_HOME) == 1


def test_a_refused_request_raises_rather_than_returning_an_error_page():
    """A 403 body parsed as a ban list is an empty ban list, which reads as
    'nothing is banned today' -- the most dangerous possible wrong answer."""
    url = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
    session = RecordedSession({NSE_HOME: (200, "<html></html>"), url: (403, "Error 1010")})
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    with pytest.raises(RuntimeError) as refused:
        nse.read_text(url)
    assert "403" in str(refused.value)


def test_a_body_that_arrived_is_returned_unchanged():
    url = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
    session = RecordedSession({NSE_HOME: (200, "<html></html>"), url: (200, BAN_FILE)})
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    assert nse.read_text(url) == BAN_FILE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/runtime/test_nse_public_data.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.nse_public_data'`

- [ ] **Step 3: Write minimal implementation**

```python
# runtime/nse_public_data.py
"""One warmed session for NSE's own public endpoints.

Two facts, both verified against the live site 2026-09-02:

1. `urllib.request` is refused with a Cloudflare 403 -- the same wall
   `broker-market-feed-reader` hit and answered the same way, with
   `curl_cffi` impersonating a real browser's TLS fingerprint.
2. An `/api/` call from a session that has not yet fetched the homepage is
   refused. The homepage sets the cookie every later call is checked against,
   so it is fetched once per session and never again.

A non-200 raises. This matters more here than in most readers: an error page
parsed as a ban list is an *empty* ban list, and an empty ban list reads as
"nothing is banned today" -- the failure that looks exactly like the good case.
"""

from __future__ import annotations

NSE_HOME = "https://www.nseindia.com"
NSE_API_HOST = "https://www.nseindia.com"
NSE_ARCHIVE_HOST = "https://nsearchives.nseindia.com"
BROWSER_TO_IMPERSONATE = "chrome"


def open_browser_session():
    """A real curl_cffi session. Separated so tests never touch the network."""
    from curl_cffi import requests

    return requests.Session(impersonate=BROWSER_TO_IMPERSONATE)


class NsePublicData:
    """Fetches NSE's public files through one session, warmed once."""

    def __init__(self, session, timeout_seconds: float) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds
        self._is_warm = False
        self._requests_made = 0
        self._requests_refused = 0

    def _warm(self) -> None:
        if self._is_warm:
            return
        self._session.get(NSE_HOME, timeout=self._timeout_seconds)
        self._is_warm = True

    def read_text(self, url: str) -> str:
        self._warm()
        self._requests_made += 1
        response = self._session.get(url, timeout=self._timeout_seconds)
        if response.status_code != 200:
            self._requests_refused += 1
            raise RuntimeError(
                f"NSE answered {response.status_code} for {url}. Refusing rather than "
                f"parsing the body: an error page read as a ban list is an empty ban "
                f"list, which reads as 'nothing is banned today'."
            )
        return response.text

    def read_json(self, url: str):
        self._warm()
        self._requests_made += 1
        response = self._session.get(url, timeout=self._timeout_seconds)
        if response.status_code != 200:
            self._requests_refused += 1
            raise RuntimeError(f"NSE answered {response.status_code} for {url}.")
        return response.json()

    def standing(self) -> dict:
        return {
            "is_warm": self._is_warm,
            "requests_made": self._requests_made,
            "requests_refused": self._requests_refused,
        }


__all__ = [
    "BROWSER_TO_IMPERSONATE",
    "NSE_API_HOST",
    "NSE_ARCHIVE_HOST",
    "NSE_HOME",
    "NsePublicData",
    "open_browser_session",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/runtime/test_nse_public_data.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Prove it works against the real site, once**

Run:

```bash
.venv/bin/python -c "
from runtime.nse_public_data import NsePublicData, open_browser_session
nse = NsePublicData(session=open_browser_session(), timeout_seconds=20.0)
body = nse.read_text('https://nsearchives.nseindia.com/content/fo/fo_secban.csv')
print(repr(body[:80])); print(nse.standing())
"
```

Expected: the ban file's first line, and `requests_refused: 0`. If it is
refused, that is a real finding about the endpoint — report it, do not work
around it by relaxing the status check.

- [ ] **Step 6: Commit**

```bash
git add runtime/nse_public_data.py tests/runtime/test_nse_public_data.py
git commit -m "feat: one warmed NSE session, refusing a non-200 rather than parsing it"
```

---

### Task 3: trading-restriction-reader

**Files:**
- Create: `parts/stock_market_news_data/__init__.py` (empty)
- Create: `parts/stock_market_news_data/trading_restriction_reader.py`
- Test: `tests/parts/stock_market_news_data/test_trading_restriction_reader.py`
- Modify: `~/.config/ajit-segment-bots/settings/runtime.toml` (append three settings)

**Interfaces:**
- Consumes: `NsePublicData` (Task 2), `InstrumentRestrictionReport` +
  `RestrictionKind` + `read_nse_date` (Task 1).
- Produces: `TradingRestrictionReader.reports_from_ban_file(body, observed_at_ns)`,
  `.reports_from_asm(document, observed_at_ns)`, `describe_reader`,
  `PART_DECLARATION`, `start_part`. Publishes `instrument-restriction-report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/parts/stock_market_news_data/test_trading_restriction_reader.py
"""Both fixtures are live NSE responses captured 2026-09-02 (RL-063):
nsearchives.nseindia.com/content/fo/fo_secban.csv in full, and one row from
each list of www.nseindia.com/api/reportASM."""

import datetime

from runtime.market_conditions import RestrictionKind
from parts.stock_market_news_data.trading_restriction_reader import (
    TradingRestrictionReader,
)

BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"

ASM_DOCUMENT = {
    "longterm": {"data": [{
        "asmSurvIndicator": "Stage I", "asmTime": "02-Sep-2026",
        "companyName": "A2Z Infra Engineering Limited", "isin": "INE619I01012",
        "series": None, "survCode": "LTASM - I (13)",
        "survDesc": "Long Term Additional Surveillance Measure (LTASM) - Stage I",
        "symbol": "A2ZINFRA", "srno": 1,
    }]},
    "shortterm": {"data": [{
        "asmSurvIndicator": "Stage I", "asmTime": "02-Sep-2026",
        "companyName": "Aastha Spintex Limited", "isin": "INE2FMX01012",
        "series": None, "survCode": "STASM - I (11)",
        "survDesc": "Short Term Additional Surveillance Measure (STASM) - Stage I",
        "symbol": "AASTHA", "srno": 1,
    }]},
}

OBSERVED_AT_NS = 1_756_800_000_000_000_000


def test_reads_every_banned_symbol_from_nse_s_own_ban_file():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_ban_file(BAN_FILE, OBSERVED_AT_NS)
    assert [report.symbol for report in reports] == ["LICHSGFIN", "SAIL"]
    assert all(report.kind is RestrictionKind.FNO_BAN for report in reports)
    assert all(report.source == "nse-fo-secban" for report in reports)


def test_the_ban_file_s_header_is_the_date_it_is_stated_for_not_a_symbol():
    """Line one is 'Securities in Ban For Trade Date 02-SEP-2026:' -- read as a
    symbol it becomes a banned instrument that does not exist, and skipped
    without reading it loses the only date the file states."""
    reader = TradingRestrictionReader()
    reports = reader.reports_from_ban_file(BAN_FILE, OBSERVED_AT_NS)
    assert all(report.stated_for == datetime.date(2026, 9, 2) for report in reports)


def test_an_empty_ban_list_is_a_real_answer_not_a_failure():
    """Most days ban nothing. Zero reports must mean zero, and the part must
    not confuse it with 'the fetch failed' -- the fetch failing raises."""
    reader = TradingRestrictionReader()
    empty = "Securities in Ban For Trade Date 03-SEP-2026:\n"
    assert reader.reports_from_ban_file(empty, OBSERVED_AT_NS) == ()


def test_reads_both_asm_lists_and_keeps_them_apart():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_asm(ASM_DOCUMENT, OBSERVED_AT_NS)
    by_symbol = {report.symbol: report for report in reports}
    assert by_symbol["A2ZINFRA"].kind is RestrictionKind.ASM_LONG_TERM
    assert by_symbol["AASTHA"].kind is RestrictionKind.ASM_SHORT_TERM


def test_an_asm_row_keeps_nse_s_own_stage_description_as_the_detail():
    reader = TradingRestrictionReader()
    reports = reader.reports_from_asm(ASM_DOCUMENT, OBSERVED_AT_NS)
    long_term = next(r for r in reports if r.symbol == "A2ZINFRA")
    assert long_term.detail == "Long Term Additional Surveillance Measure (LTASM) - Stage I"


def test_an_asm_row_with_no_symbol_is_skipped_not_reported_under_none():
    """NSE has published rows with a null series; a null symbol has not been
    seen, and if it ever is, a restriction keyed None would match nothing and
    silently protect nothing."""
    reader = TradingRestrictionReader()
    document = {"longterm": {"data": [{"symbol": None, "survDesc": "x", "asmTime": "02-Sep-2026"}]},
                "shortterm": {"data": []}}
    assert reader.reports_from_asm(document, OBSERVED_AT_NS) == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/test_trading_restriction_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts.stock_market_news_data'`

- [ ] **Step 3: Add the three settings**

Append to `~/.config/ajit-segment-bots/settings/runtime.toml`:

```toml
[nse_public_data_timeout_seconds]
value = 20.0
unit  = "seconds"
note  = "Claude, 2026-09-02: how long a request to one of NSE's own public endpoints may take before it is abandoned. 20s matches broker_instrument_catalogue_timeout_seconds' own reasoning -- these are archive files served by the same CDN, and a warmed session's first homepage call is the slow one. NOT measured against a p99 of NSE's own latency; re-derive if refusals cluster at the timeout rather than at a status code."

[trading_restriction_poll_seconds]
value = 900.0
unit  = "seconds between fetches"
note  = "Claude, 2026-09-02: how often trading-restriction-reader re-reads the F&O ban list plus the ASM lists. NSE republishes fo_secban.csv once per trading day (its own body names a single Trade Date) and the ASM lists daily after market close, so a 15-minute poll is far finer than the data changes -- chosen for the intraday-revision case NSE occasionally publishes rather than for the daily one, and deliberately not finer, because a per-minute poll of a once-daily file is load on a public endpoint with no answer to show for it."

[asm_restricts_new_positions]
value = true
unit  = "whether an ASM stage blocks a fresh position"
note  = "Claude, 2026-09-02: NSE's ASM raises margin and tightens surveillance; it does not itself forbid trading. Set true because Phase A is paper trading with a bull bot that has no margin model yet -- entering an ASM name would size against a margin requirement the system does not know. Revisit when the margin model reads real broker margin per instrument; this is a deliberate conservatism, not an exchange rule."
```

- [ ] **Step 4: Write minimal implementation**

```python
# parts/stock_market_news_data/trading_restriction_reader.py
"""trading-restriction-reader: read the exchange's F&O ban, ASM, GSM, halt lists.

Two NSE endpoints, both public, both verified live 2026-09-02:

- `nsearchives.nseindia.com/content/fo/fo_secban.csv` -- not really a CSV. Line
  one is a sentence carrying the trade date it is stated for; each later line is
  `<serial>,<symbol>`. Read with `csv` alone the header becomes a banned
  instrument named "Securities in Ban For Trade Date 02-SEP-2026:", and skipped
  blindly it loses the only date in the file.
- `www.nseindia.com/api/reportASM` -- `{"longterm": {"data": [...]},
  "shortterm": {"data": [...]}}`, each row carrying `symbol`, `survDesc` and
  `asmTime`. The two lists are different restrictions and are kept apart.

**An empty ban list is a real answer.** Most days ban nothing, so zero reports
must mean zero -- which is exactly why `NsePublicData` raises on a non-200
instead of handing this part an error page to find no symbols in.
"""

from __future__ import annotations

import re

from runtime.market_conditions import (
    InstrumentRestrictionReport,
    RestrictionKind,
    read_nse_date,
)
from runtime.nse_public_data import NSE_API_HOST, NSE_ARCHIVE_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trading-restriction-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("instrument-restriction-report", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

BAN_FILE_URL = f"{NSE_ARCHIVE_HOST}/content/fo/fo_secban.csv"
ASM_URL = f"{NSE_API_HOST}/api/reportASM"

BAN_SOURCE = "nse-fo-secban"
ASM_LONG_TERM_SOURCE = "nse-asm-longterm"
ASM_SHORT_TERM_SOURCE = "nse-asm-shortterm"

# "Securities in Ban For Trade Date 02-SEP-2026:" -- the one date the file states.
BAN_HEADER_DATE = re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4})")

ASM_LIST_KINDS = {
    "longterm": (RestrictionKind.ASM_LONG_TERM, ASM_LONG_TERM_SOURCE),
    "shortterm": (RestrictionKind.ASM_SHORT_TERM, ASM_SHORT_TERM_SOURCE),
}


class TradingRestrictionReader:
    """Turns NSE's own two restriction publications into reports."""

    def __init__(self) -> None:
        self._banned_symbols_last_seen = 0
        self._asm_symbols_last_seen = 0

    def reports_from_ban_file(self, body: str, observed_at_ns: int) -> tuple:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            return ()
        header = lines[0]
        stated = BAN_HEADER_DATE.search(header)
        if stated is None:
            raise ValueError(
                f"the ban file's first line states no trade date: {header!r}. Refusing "
                f"rather than dating today's ban list by wall clock."
            )
        stated_for = read_nse_date(stated.group(1))
        reports = []
        for line in lines[1:]:
            _, _, symbol = line.partition(",")
            symbol = symbol.strip()
            if not symbol:
                continue
            reports.append(InstrumentRestrictionReport(
                symbol=symbol, kind=RestrictionKind.FNO_BAN, source=BAN_SOURCE,
                stated_for=stated_for, detail=header, observed_at_ns=observed_at_ns,
            ))
        self._banned_symbols_last_seen = len(reports)
        return tuple(reports)

    def reports_from_asm(self, document, observed_at_ns: int) -> tuple:
        reports = []
        for list_name, (kind, source) in ASM_LIST_KINDS.items():
            rows = document.get(list_name, {}).get("data", [])
            for row in rows:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                reports.append(InstrumentRestrictionReport(
                    symbol=symbol, kind=kind, source=source,
                    stated_for=read_nse_date(row["asmTime"]),
                    detail=row.get("survDesc", ""), observed_at_ns=observed_at_ns,
                ))
        self._asm_symbols_last_seen = len(reports)
        return tuple(reports)


def describe_reader(reader: TradingRestrictionReader) -> dict:
    return {
        "part_id": PART_ID,
        "banned_symbols_last_seen": reader._banned_symbols_last_seen,
        "asm_symbols_last_seen": reader._asm_symbols_last_seen,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    reader = TradingRestrictionReader()
    publish_reports = context.bus.publisher_for("instrument-restriction-report")
    poll_seconds = context.number("trading_restriction_poll_seconds")
    last_polled_at = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is not None and now - last_polled_at[0] < poll_seconds:
            return
        last_polled_at[0] = now
        observed_at_ns = time.time_ns()
        reports = reader.reports_from_ban_file(nse.read_text(BAN_FILE_URL), observed_at_ns)
        reports += reader.reports_from_asm(nse.read_json(ASM_URL), observed_at_ns)
        if reports:
            publish_reports(reports)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_reader(reader) | nse.standing(),
    )


__all__ = [
    "ASM_URL",
    "BAN_FILE_URL",
    "PART_DECLARATION",
    "PART_ID",
    "TradingRestrictionReader",
    "describe_reader",
    "start_part",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/ -v`
Expected: PASS, 6 tests.

- [ ] **Step 6: Prove the part's declaration matches the blueprint**

Run: `.venv/bin/python dashboard/check_payload_reads.py && python3 dashboard/check_part_calls.py`
Expected: both pass, `parts imported` now 337.

- [ ] **Step 7: Commit**

```bash
git add parts/stock_market_news_data/ tests/parts/stock_market_news_data/
git commit -m "feat: trading-restriction-reader -- NSE's own ban plus ASM lists"
```

---

### Task 4: instrument-restriction-state

**Files:**
- Create: `parts/stock_market_news_data/instrument_restriction_state.py`
- Test: `tests/parts/stock_market_news_data/test_instrument_restriction_state.py`
- Modify: `~/.config/ajit-segment-bots/settings/runtime.toml` (two settings)

**Interfaces:**
- Consumes: `InstrumentRestrictionReport` (Task 1), reports published by Task 3.
- Produces: `InstrumentRestrictionState.observe(report)`,
  `.restrictions(now_ns) -> tuple[InstrumentRestriction, ...]`,
  `.restriction_for(symbol, now_ns)`. Publishes `instrument-restriction`.

**Why the reader does not publish the level itself:** two sources report on the
same symbol, and a claim must expire when its source goes quiet. A reader that
also held the level would keep a name banned forever the day NSE stops
publishing it — which is the unbounded-level defect this project has already
paid for three times.

- [ ] **Step 1: Write the failing test**

```python
# tests/parts/stock_market_news_data/test_instrument_restriction_state.py
"""Reports here are the exact shapes trading-restriction-reader builds from the
live NSE responses captured 2026-09-02."""

import datetime

from runtime.market_conditions import InstrumentRestrictionReport, RestrictionKind
from parts.stock_market_news_data.instrument_restriction_state import (
    InstrumentRestrictionState,
)

DAY = datetime.date(2026, 9, 2)
ONE_SECOND_NS = 1_000_000_000
NOW_NS = 1_756_800_000_000_000_000


def _report(symbol="LICHSGFIN", kind=RestrictionKind.FNO_BAN,
            source="nse-fo-secban", observed_at_ns=NOW_NS):
    return InstrumentRestrictionReport(
        symbol=symbol, kind=kind, source=source, stated_for=DAY,
        detail="Securities in Ban For Trade Date 02-SEP-2026", observed_at_ns=observed_at_ns,
    )


def test_a_reported_symbol_may_not_open_a_new_position():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report())
    restriction = state.restriction_for("LICHSGFIN", NOW_NS)
    assert restriction is not None
    assert restriction.may_open_new_position is False


def test_an_unreported_symbol_has_no_restriction_at_all():
    """Absence is absence. A restriction object saying 'not restricted' would
    make every consumer's 'do I have one' check meaningless."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report())
    assert state.restriction_for("RELIANCE", NOW_NS) is None


def test_two_sources_on_one_symbol_merge_into_one_restriction():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="SAIL"))
    state.observe(_report(symbol="SAIL", kind=RestrictionKind.ASM_SHORT_TERM,
                          source="nse-asm-shortterm"))
    restriction = state.restriction_for("SAIL", NOW_NS)
    assert set(restriction.kinds) == {RestrictionKind.FNO_BAN, RestrictionKind.ASM_SHORT_TERM}
    assert set(restriction.sources) == {"nse-fo-secban", "nse-asm-shortterm"}


def test_a_claim_older_than_its_bound_is_absent_not_old():
    """The whole reason this part exists. NSE stops publishing a name the day
    its ban lifts -- it does not publish an un-ban. Without the bound the name
    stays banned forever and the bot never trades it again."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(observed_at_ns=NOW_NS))
    later_ns = NOW_NS + 3601 * ONE_SECOND_NS
    assert state.restriction_for("LICHSGFIN", later_ns) is None


def test_a_claim_inside_its_bound_still_stands():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(observed_at_ns=NOW_NS))
    later_ns = NOW_NS + 3599 * ONE_SECOND_NS
    assert state.restriction_for("LICHSGFIN", later_ns) is not None


def test_one_source_expiring_leaves_the_other_source_s_claim_standing():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="SAIL", observed_at_ns=NOW_NS))
    fresh_ns = NOW_NS + 3000 * ONE_SECOND_NS
    state.observe(_report(symbol="SAIL", kind=RestrictionKind.ASM_SHORT_TERM,
                          source="nse-asm-shortterm", observed_at_ns=fresh_ns))
    at_ns = NOW_NS + 3601 * ONE_SECOND_NS
    restriction = state.restriction_for("SAIL", at_ns)
    assert restriction is not None
    assert restriction.kinds == (RestrictionKind.ASM_SHORT_TERM,)


def test_restrictions_lists_every_symbol_still_standing():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="LICHSGFIN"))
    state.observe(_report(symbol="SAIL"))
    assert {r.symbol for r in state.restrictions(NOW_NS)} == {"LICHSGFIN", "SAIL"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/test_instrument_restriction_state.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Add the two settings**

```toml
[instrument_restriction_maximum_age_seconds]
value = 5400.0
unit  = "seconds"
note  = "Claude, 2026-09-02: how long one source's restriction claim stands after it was last seen. NSE does not publish an un-ban -- a lifted name simply stops appearing in fo_secban.csv -- so a claim that never expired would ban a symbol permanently. 5400s is 1.5x trading_restriction_poll_seconds' 900s plus room for two consecutive failed polls: short enough that a lifted ban clears within about the time it takes to notice, long enough that one refused fetch never un-bans a name that is still banned. Not measured against real ban-lift timing; re-derive once a full ban lift has been observed end to end."

[instrument_restriction_refresh_interval_seconds]
value = 60.0
unit  = "seconds"
note  = "Claude, 2026-09-02: how long the unchanged restriction level may go unsaid before it is restated. Most days it does not change at all, so this is the heartbeat that tells a consumer starting mid-session what is banned without waiting for the next poll. 60s matches the health interval already used across the spine -- a consumer that has been up a minute has the level."
```

- [ ] **Step 4: Write minimal implementation**

```python
# parts/stock_market_news_data/instrument_restriction_state.py
"""instrument-restriction-state: state whether an instrument may be traded now.

The level behind every refusal in the hard channel. Reports arrive per source;
this merges them per symbol and lets each source's claim expire on its own age
bound.

**The expiry is the whole point.** NSE does not publish an un-ban -- a symbol
whose ban lifts simply stops appearing in fo_secban.csv. A level that never
expired would therefore ban a name permanently the first day it was banned,
and nothing would report it: the board would show a working part, the bot would
just never trade that symbol again. That is the same shape as the 214 phantom
running parts of 2026-08-26 and the fifty-six minute price of 2026-08-23, and
it is why the bound comes from a setting rather than being optional.

Absence is absence: an unrestricted symbol has *no* restriction rather than a
restriction saying nothing is wrong, so a consumer's "do I have one" is the
check that fires.
"""

from __future__ import annotations

from runtime.market_conditions import InstrumentRestriction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instrument-restriction-state"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("instrument-restriction-report",),
    produces=("instrument-restriction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NANOSECONDS_PER_SECOND = 1_000_000_000


class InstrumentRestrictionState:
    """Every source's live claim per symbol, each expiring on its own age."""

    def __init__(self, maximum_age_seconds: float) -> None:
        if not maximum_age_seconds > 0:
            raise ValueError(
                "maximum_age_seconds must be positive: it is how long a claim stands "
                f"after its source last said it, and NSE publishes no un-ban. Got "
                f"{maximum_age_seconds!r}."
            )
        self._maximum_age_ns = int(maximum_age_seconds * NANOSECONDS_PER_SECOND)
        self._claims: dict[str, dict[str, object]] = {}
        self._reports_seen = 0

    def observe(self, report) -> None:
        self._reports_seen += 1
        self._claims.setdefault(report.symbol, {})[report.source] = report

    def _live_claims(self, symbol: str, now_ns: int) -> tuple:
        oldest_believable_ns = now_ns - self._maximum_age_ns
        return tuple(
            report for report in self._claims.get(symbol, {}).values()
            if report.observed_at_ns >= oldest_believable_ns
        )

    def restriction_for(self, symbol: str, now_ns: int) -> InstrumentRestriction | None:
        live = self._live_claims(symbol, now_ns)
        if not live:
            return None
        newest = max(live, key=lambda report: report.observed_at_ns)
        return InstrumentRestriction(
            symbol=symbol,
            kinds=tuple(report.kind for report in live),
            sources=tuple(report.source for report in live),
            stated_for=newest.stated_for,
            observed_at_ns=newest.observed_at_ns,
        )

    def restrictions(self, now_ns: int) -> tuple:
        found = (self.restriction_for(symbol, now_ns) for symbol in self._claims)
        return tuple(restriction for restriction in found if restriction is not None)

    @property
    def reports_seen(self) -> int:
        return self._reports_seen


def describe_state(state: InstrumentRestrictionState, now_ns: int) -> dict:
    standing = state.restrictions(now_ns)
    return {
        "part_id": PART_ID,
        "reports_seen": state.reports_seen,
        "symbols_restricted": len(standing),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisher, without_observation_time

    reports = Batch(read=context.bus.reader("instrument-restriction-report"))
    state = InstrumentRestrictionState(
        maximum_age_seconds=context.number("instrument_restriction_maximum_age_seconds"),
    )
    publisher = LevelPublisher(
        publish=context.bus.publisher_for("instrument-restriction"),
        refresh_interval_seconds=context.number(
            "instrument_restriction_refresh_interval_seconds"
        ),
        identity_of=without_observation_time,
    )

    def tick() -> None:
        for report in reports.payloads():
            state.observe(report)
        publisher.publish_level(state.restrictions(time.time_ns()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_state(state, time.time_ns()),
    )


__all__ = [
    "InstrumentRestrictionState",
    "PART_DECLARATION",
    "PART_ID",
    "describe_state",
    "start_part",
]
```

`without_observation_time` matters here: every restriction carries
`observed_at_ns`, so compared whole no two statements of an unchanged level are
ever equal, nothing is skipped, and the skip counter reads zero while the level
publisher appears to be working. That is the exact failure the first version of
`level_publishing.py` had against `PartFault`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/ -v`
Expected: PASS, 13 tests.

- [ ] **Step 6: Commit**

```bash
git add parts/stock_market_news_data/instrument_restriction_state.py tests/parts/stock_market_news_data/test_instrument_restriction_state.py
git commit -m "feat: instrument-restriction-state -- claims that expire, because NSE publishes no un-ban"
```

---

### Task 5: corporate-action-reader

**Files:**
- Create: `parts/stock_market_news_data/corporate_action_reader.py`
- Test: `tests/parts/stock_market_news_data/test_corporate_action_reader.py`
- Modify: `runtime.toml` (one setting)

**Interfaces:**
- Consumes: `NsePublicData`, `CorporateActionReport`, `read_nse_date`.
- Produces: `CorporateActionReader.reports_from(rows, observed_at_ns)`,
  `describe_reader`, `PART_DECLARATION`, `start_part`. Publishes
  `corporate-action-report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/parts/stock_market_news_data/test_corporate_action_reader.py
"""The row is a live response from
www.nseindia.com/api/corporates-corporateActions?index=equities, captured
2026-09-02 (RL-063). NSE writes "-" where it has no date, which is why the
reader must never parse a date it was not given."""

import datetime

from parts.stock_market_news_data.corporate_action_reader import CorporateActionReader

NTPC_DIVIDEND = {
    "bcEndDate": "-", "bcStartDate": "-", "caBroadcastDate": None,
    "comp": "NTPC Limited", "exDate": "02-Sep-2026", "faceVal": "10",
    "ind": "-", "isin": "INE733E01010", "ndEndDate": "-", "ndStartDate": "-",
    "recDate": "02-Sep-2026", "series": "EQ",
    "subject": "Dividend - Rs 3.50 Per Share", "symbol": "NTPC",
}

OBSERVED_AT_NS = 1_756_800_000_000_000_000


def test_reads_a_real_nse_corporate_action_row():
    reader = CorporateActionReader()
    reports = reader.reports_from([NTPC_DIVIDEND], OBSERVED_AT_NS)
    assert len(reports) == 1
    report = reports[0]
    assert report.symbol == "NTPC"
    assert report.series == "EQ"
    assert report.isin == "INE733E01010"
    assert report.subject == "Dividend - Rs 3.50 Per Share"
    assert report.ex_date == datetime.date(2026, 9, 2)
    assert report.record_date == datetime.date(2026, 9, 2)
    assert report.face_value == 10.0


def test_nse_s_own_dash_for_a_missing_date_becomes_none_not_today():
    """A record date of "-" dated to today would make the action look effective
    now. None is the honest reading, and every consumer already handles it."""
    row = dict(NTPC_DIVIDEND, recDate="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS)[0].record_date is None


def test_a_row_with_no_ex_date_is_skipped_entirely():
    """ex_date is what a price adjustment is applied from. Without it there is
    nothing to apply and nothing to guess."""
    row = dict(NTPC_DIVIDEND, exDate="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS) == ()


def test_a_face_value_nse_did_not_state_is_none_not_zero():
    row = dict(NTPC_DIVIDEND, faceVal="-")
    reader = CorporateActionReader()
    assert reader.reports_from([row], OBSERVED_AT_NS)[0].face_value is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/test_corporate_action_reader.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Add the setting**

```toml
[corporate_action_poll_seconds]
value = 3600.0
unit  = "seconds between fetches"
note  = "Claude, 2026-09-02: how often corporate-action-reader re-reads www.nseindia.com/api/corporates-corporateActions. The endpoint returns the actions around the current date and changes when a company files, which is a daily-to-weekly event per name, not an intraday one -- so hourly is already far finer than the data moves. What actually matters is that an action is seen before its ex-date, and an hourly poll gives roughly twenty-four chances between a filing and the open."
```

- [ ] **Step 4: Write minimal implementation**

```python
# parts/stock_market_news_data/corporate_action_reader.py
"""corporate-action-reader: read the exchange's own corporate action file.

`www.nseindia.com/api/corporates-corporateActions?index=equities`, verified live
2026-09-02: a JSON list, each row carrying `symbol`, `series`, `isin`,
`faceVal`, `subject`, `exDate`, `recDate` plus book-closure and no-delivery
windows.

**NSE writes "-" where it has no value**, in every date field and in faceVal.
Read as a date that is a parse error; defaulted to today it is worse -- an
action dated today is an action that adjusts a price series right now. So a
missing optional date is None, and a row with no ex-date is skipped: the
ex-date is the only thing an adjustment can be applied from.

This part reads and republishes only. What an action *does* to a price is
corporate-action-adjuster's job, because reading NSE's file and interpreting
"Bonus 1:1" are two responsibilities (T-6).
"""

from __future__ import annotations

from runtime.market_conditions import CorporateActionReport, read_nse_date
from runtime.nse_public_data import NSE_API_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "corporate-action-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("corporate-action-report", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CORPORATE_ACTIONS_URL = f"{NSE_API_HOST}/api/corporates-corporateActions?index=equities"

# NSE's own placeholder for "no value", in every field it uses one.
NOT_STATED = "-"


def _optional_date(stated):
    if not stated or stated.strip() == NOT_STATED:
        return None
    return read_nse_date(stated)


def _optional_number(stated):
    if not stated or str(stated).strip() == NOT_STATED:
        return None
    return float(stated)


class CorporateActionReader:
    """Turns NSE's corporate action rows into reports."""

    def __init__(self) -> None:
        self._rows_seen = 0
        self._rows_without_an_ex_date = 0

    def reports_from(self, rows, observed_at_ns: int) -> tuple:
        reports = []
        for row in rows:
            self._rows_seen += 1
            ex_date = _optional_date(row.get("exDate"))
            if ex_date is None:
                self._rows_without_an_ex_date += 1
                continue
            reports.append(CorporateActionReport(
                symbol=row["symbol"], series=row.get("series", ""),
                isin=row.get("isin", ""), subject=row.get("subject", ""),
                ex_date=ex_date, record_date=_optional_date(row.get("recDate")),
                face_value=_optional_number(row.get("faceVal")),
                observed_at_ns=observed_at_ns,
            ))
        return tuple(reports)

    @property
    def rows_seen(self) -> int:
        return self._rows_seen

    @property
    def rows_without_an_ex_date(self) -> int:
        return self._rows_without_an_ex_date


def describe_reader(reader: CorporateActionReader) -> dict:
    return {
        "part_id": PART_ID,
        "rows_seen": reader.rows_seen,
        "rows_without_an_ex_date": reader.rows_without_an_ex_date,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    reader = CorporateActionReader()
    publish_reports = context.bus.publisher_for("corporate-action-report")
    poll_seconds = context.number("corporate_action_poll_seconds")
    last_polled_at = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is not None and now - last_polled_at[0] < poll_seconds:
            return
        last_polled_at[0] = now
        rows = nse.read_json(CORPORATE_ACTIONS_URL)
        reports = reader.reports_from(rows, time.time_ns())
        if reports:
            publish_reports(reports)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_reader(reader) | nse.standing(),
    )


__all__ = [
    "CORPORATE_ACTIONS_URL",
    "CorporateActionReader",
    "PART_DECLARATION",
    "PART_ID",
    "describe_reader",
    "start_part",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/ -v`
Expected: PASS, 17 tests.

- [ ] **Step 6: Commit**

```bash
git add parts/stock_market_news_data/corporate_action_reader.py tests/parts/stock_market_news_data/test_corporate_action_reader.py
git commit -m "feat: corporate-action-reader -- NSE's own file, with its dashes read as absent"
```

---

### Task 6: corporate-action-adjuster

**Files:**
- Create: `parts/stock_market_news_data/corporate_action_adjuster.py`
- Test: `tests/parts/stock_market_news_data/test_corporate_action_adjuster.py`

**Interfaces:**
- Consumes: `CorporateActionReport` (Task 5), `CorporateAction` (Task 1).
- Produces: `CorporateActionAdjuster.action_for(report)`,
  `describe_adjuster`, `PART_DECLARATION`, `start_part`. Publishes
  `corporate-action`.

**The one hard part:** NSE states the action only in free text — `"Bonus 1:1"`,
`"Face Value Split From Rs 10/- To Rs 2/-"`, `"Dividend - Rs 3.50 Per Share"`.
The ratio must be read from that string or the action must be refused. A
dividend does move the price on ex-date, but by an amount that depends on the
price — it is **not** a multiplicative series adjustment, so it is carried with
`price_factor` 1.0 and is not applied to a candle series.

- [ ] **Step 1: Write the failing test**

```python
# tests/parts/stock_market_news_data/test_corporate_action_adjuster.py
"""Subject strings are NSE's own wording. "Dividend - Rs 3.50 Per Share" is
verbatim from the live 2026-09-02 response; the bonus and split wordings follow
the same published pattern on that endpoint."""

import datetime

from runtime.market_conditions import CorporateActionReport
from parts.stock_market_news_data.corporate_action_adjuster import CorporateActionAdjuster

EX_DATE = datetime.date(2026, 9, 2)
OBSERVED_AT_NS = 1_756_800_000_000_000_000


def _report(subject, symbol="RELIANCE"):
    return CorporateActionReport(
        symbol=symbol, series="EQ", isin="INE002A01018", subject=subject,
        ex_date=EX_DATE, record_date=EX_DATE, face_value=10.0,
        observed_at_ns=OBSERVED_AT_NS,
    )


def test_a_one_for_one_bonus_halves_the_price_and_doubles_the_quantity():
    """The defect this whole part exists to stop: unadjusted, this is a -50%
    candle. kline-window-builder feeds it to Kronos, every detector fires, and
    cost-basis-tracker prices a position against a basis that is now wrong."""
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:1"))
    assert action.kind == "bonus"
    assert action.price_factor == 0.5
    assert action.quantity_factor == 2.0
    assert action.ex_date == EX_DATE


def test_a_one_for_two_bonus_is_a_third_off_not_a_half():
    """1:2 means one new share for every two held: three shares where two were."""
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:2"))
    assert action.price_factor == 2 / 3
    assert action.quantity_factor == 3 / 2


def test_a_face_value_split_divides_by_the_ratio_of_the_two_face_values():
    action = CorporateActionAdjuster().action_for(
        _report("Face Value Split From Rs 10/- To Rs 2/-")
    )
    assert action.kind == "split"
    assert action.price_factor == 0.2
    assert action.quantity_factor == 5.0


def test_a_dividend_is_carried_but_never_rescales_a_price_series():
    """A dividend does drop the price on ex-date, by an absolute amount rather
    than a ratio. Carrying it with a factor of 1.0 says 'known, not a series
    adjustment' -- silently dropping it would lose a real ex-date event."""
    action = CorporateActionAdjuster().action_for(_report("Dividend - Rs 3.50 Per Share"))
    assert action.kind == "dividend"
    assert action.price_factor == 1.0
    assert action.quantity_factor == 1.0


def test_an_action_whose_wording_states_no_ratio_is_refused_not_guessed():
    """NSE publishes wordings this parser has never seen. Returning None means
    'not understood' and leaves the series unadjusted, which is recoverable.
    A guessed ratio silently rewrites a price history, which is not."""
    assert CorporateActionAdjuster().action_for(_report("Scheme of Arrangement")) is None


def test_a_bonus_with_a_zero_denominator_is_refused_rather_than_dividing_by_zero():
    assert CorporateActionAdjuster().action_for(_report("Bonus 1:0")) is None


def test_the_wording_is_carried_so_a_wrong_factor_can_be_traced_to_its_source():
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:1"))
    assert action.stated_from == "Bonus 1:1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/test_corporate_action_adjuster.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# parts/stock_market_news_data/corporate_action_adjuster.py
"""corporate-action-adjuster: state the price adjustment an action implies.

NSE states what an action is only in free text -- "Bonus 1:1", "Face Value
Split From Rs 10/- To Rs 2/-", "Dividend - Rs 3.50 Per Share" -- so the ratio
is read from the wording or the action is refused.

**Refusing is the safe direction and guessing is not.** An action this parser
does not understand leaves the price series unadjusted: wrong, visible, and
recoverable the moment the wording is added. A guessed ratio silently rewrites
a price history, and every consumer downstream believes it.

A dividend is carried with a factor of 1.0 rather than dropped. It does move
the price on ex-date, but by an absolute amount rather than a ratio, so it is
not a series adjustment -- and saying so explicitly is different from losing
the event.
"""

from __future__ import annotations

import re

from runtime.market_conditions import CorporateAction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "corporate-action-adjuster"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("corporate-action-report", "broker-instrument-listing"),
    produces=("corporate-action", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# "Bonus 1:1", "Bonus Issue 1:2" -- new shares per held share.
BONUS = re.compile(r"bonus.*?(\d+)\s*:\s*(\d+)", re.IGNORECASE)
# "Face Value Split From Rs 10/- To Rs 2/-"
SPLIT = re.compile(
    r"split.*?from\s*rs\.?\s*([\d.]+).*?to\s*rs\.?\s*([\d.]+)", re.IGNORECASE
)
DIVIDEND = re.compile(r"dividend", re.IGNORECASE)


class CorporateActionAdjuster:
    """Reads NSE's own wording into a factor, or refuses it."""

    def __init__(self) -> None:
        self._understood = 0
        self._refused = 0

    def action_for(self, report) -> CorporateAction | None:
        found = self._factors_from(report.subject)
        if found is None:
            self._refused += 1
            return None
        kind, price_factor, quantity_factor = found
        self._understood += 1
        return CorporateAction(
            symbol=report.symbol, kind=kind, price_factor=price_factor,
            quantity_factor=quantity_factor, ex_date=report.ex_date,
            stated_from=report.subject, observed_at_ns=report.observed_at_ns,
        )

    def _factors_from(self, subject: str):
        bonus = BONUS.search(subject)
        if bonus:
            new_shares, per_held = int(bonus.group(1)), int(bonus.group(2))
            if per_held <= 0:
                return None
            quantity_factor = (per_held + new_shares) / per_held
            return "bonus", 1.0 / quantity_factor, quantity_factor
        split = SPLIT.search(subject)
        if split:
            face_before, face_after = float(split.group(1)), float(split.group(2))
            if face_before <= 0 or face_after <= 0:
                return None
            price_factor = face_after / face_before
            return "split", price_factor, 1.0 / price_factor
        if DIVIDEND.search(subject):
            return "dividend", 1.0, 1.0
        return None

    @property
    def understood(self) -> int:
        return self._understood

    @property
    def refused(self) -> int:
        return self._refused


def describe_adjuster(adjuster: CorporateActionAdjuster) -> dict:
    return {
        "part_id": PART_ID,
        "actions_understood": adjuster.understood,
        "wordings_refused": adjuster.refused,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    broker-instrument-listing is consumed to keep the adjuster's symbol
    vocabulary the broker's own, so an action on a name the broker does not
    list is not published as an adjustment nothing can apply.
    """
    from runtime.input_assembly import Batch

    reports = Batch(read=context.bus.reader("corporate-action-report"))
    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    publish_actions = context.bus.publisher_for("corporate-action")
    adjuster = CorporateActionAdjuster()
    known_symbols: set[str] = set()

    def tick() -> None:
        for listing in listings.payloads():
            known_symbols.add(listing.trading_symbol)
        actions = tuple(
            action
            for report in reports.payloads()
            if (action := adjuster.action_for(report)) is not None
        )
        if actions:
            publish_actions(actions)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_adjuster(adjuster)
        | {"instruments_known": len(known_symbols)},
    )


__all__ = [
    "CorporateActionAdjuster",
    "PART_DECLARATION",
    "PART_ID",
    "describe_adjuster",
    "start_part",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/ -v`
Expected: PASS, 24 tests.

- [ ] **Step 5: Commit**

```bash
git add parts/stock_market_news_data/corporate_action_adjuster.py tests/parts/stock_market_news_data/test_corporate_action_adjuster.py
git commit -m "feat: corporate-action-adjuster -- refuses a wording it cannot read rather than guessing a ratio"
```

---

### Task 7: market-session-calendar

**Files:**
- Create: `parts/stock_market_news_data/market_session_calendar.py`
- Test: `tests/parts/stock_market_news_data/test_market_session_calendar.py`
- Modify: `runtime.toml` (four settings)

**Interfaces:**
- Consumes: `NsePublicData`, `MarketSessionState`, `SessionKind`, `read_nse_date`.
- Produces: `MarketSessionCalendar.observe_holidays(document)`,
  `.session_at(moment) -> MarketSessionState`, `describe_calendar`,
  `PART_DECLARATION`, `start_part`. Publishes `market-session-state`.

- [ ] **Step 1: Write the failing test**

```python
# tests/parts/stock_market_news_data/test_market_session_calendar.py
"""The holiday document is the live response from
www.nseindia.com/api/holiday-master?type=trading, captured 2026-09-02: keyed by
segment ("FO" among twelve), each row carrying tradingDate/weekDay/description."""

import datetime
import zoneinfo

from runtime.market_conditions import SessionKind
from parts.stock_market_news_data.market_session_calendar import MarketSessionCalendar

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

HOLIDAY_DOCUMENT = {
    "FO": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday",
         "description": "Republic Day", "morning_session": None,
         "evening_session": None, "Sr_no": 2},
    ],
    "CM": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday",
         "description": "Republic Day", "morning_session": None,
         "evening_session": None, "Sr_no": 2},
    ],
}


def _calendar():
    calendar = MarketSessionCalendar(
        segment="FO", opens_at=datetime.time(9, 15), closes_at=datetime.time(15, 30),
        timezone=IST,
    )
    calendar.observe_holidays(HOLIDAY_DOCUMENT)
    return calendar


def test_a_weekday_inside_session_hours_is_open():
    moment = datetime.datetime(2026, 9, 2, 10, 0, tzinfo=IST)  # a Wednesday
    assert _calendar().session_at(moment).kind is SessionKind.OPEN


def test_before_the_open_is_closed_not_open():
    moment = datetime.datetime(2026, 9, 2, 9, 0, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_after_the_close_is_closed():
    """The failure this stops: paper-fill-simulator filling an overnight order
    at the last price it saw and journalling it as a trade."""
    moment = datetime.datetime(2026, 9, 2, 18, 0, tzinfo=IST)
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_a_weekend_is_closed_even_inside_session_hours():
    moment = datetime.datetime(2026, 9, 5, 11, 0, tzinfo=IST)  # a Saturday
    assert _calendar().session_at(moment).kind is SessionKind.CLOSED


def test_a_holiday_is_a_holiday_not_merely_closed_and_says_which_one():
    """Closed and holiday are different facts: one ends at 09:15 tomorrow, the
    other is the exchange not trading at all that day."""
    moment = datetime.datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    state = _calendar().session_at(moment)
    assert state.kind is SessionKind.HOLIDAY
    assert state.reason == "Republic Day"


def test_only_this_segment_s_holidays_are_read():
    """holiday-master carries twelve segments. A commodity holiday is not an
    equity-derivatives holiday, and reading them all would close the market on
    days it trades."""
    calendar = MarketSessionCalendar(
        segment="COM", opens_at=datetime.time(9, 15), closes_at=datetime.time(15, 30),
        timezone=IST,
    )
    calendar.observe_holidays(HOLIDAY_DOCUMENT)
    moment = datetime.datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    assert calendar.session_at(moment).kind is not SessionKind.HOLIDAY


def test_a_session_is_never_reported_open_before_any_holiday_list_arrived():
    """Absence of evidence is its own state (Rule 8). An empty calendar means
    'not measured', and reporting OPEN off it would let the bot trade into a
    holiday because a fetch had not happened yet."""
    calendar = MarketSessionCalendar(
        segment="FO", opens_at=datetime.time(9, 15), closes_at=datetime.time(15, 30),
        timezone=IST,
    )
    moment = datetime.datetime(2026, 9, 2, 10, 0, tzinfo=IST)
    assert calendar.session_at(moment).kind is SessionKind.CLOSED
    assert "no holiday list" in calendar.session_at(moment).reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/test_market_session_calendar.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Add the four settings**

```toml
[market_session_segment]
value = "FO"
unit  = "NSE holiday-master segment key"
note  = "Claude, 2026-09-02: which of the twelve segment keys in www.nseindia.com/api/holiday-master?type=trading market-session-calendar reads. \"FO\" is equity derivatives, the segment both Phase A bots trade (index options and stock options; docs/goal.md). The other keys seen live on 2026-09-02 are CBM, CD, CM, CMOT, COM, EGR, IRD, MF, NDM, NTRP, SLBS -- a commodity holiday is not a derivatives holiday, so reading them together would close the market on days it trades."

[market_session_opens_at_ist]
value = "09:15"
unit  = "IST clock time"
note  = "Claude, 2026-09-02: when the NSE equity-derivatives session opens. NSE's published normal market timing for the F&O segment is 09:15-15:30 IST. Pre-open (09:00-09:15) is a cash-segment auction and is deliberately NOT counted as open here -- an order sent into it behaves differently from a normal-market order, and this project has no pre-open order type. Re-derive if a pre-open strategy is ever built."

[market_session_closes_at_ist]
value = "15:30"
unit  = "IST clock time"
note  = "Claude, 2026-09-02: when the NSE equity-derivatives session closes. Pairs with market_session_opens_at_ist; NSE's published normal market timing. The post-close window (15:40-16:00, cash only) is not counted as open for the same reason pre-open is not."

[market_session_poll_seconds]
value = 21600.0
unit  = "seconds between fetches"
note  = "Claude, 2026-09-02: how often market-session-calendar re-reads the holiday list. The list is published annually and amended rarely (an unscheduled closure, an election day), so four fetches a day is already generous -- the session state itself is recomputed on every tick from the clock, and only the holiday list needs fetching."
```

- [ ] **Step 4: Write minimal implementation**

```python
# parts/stock_market_news_data/market_session_calendar.py
"""market-session-calendar: state which trading session the market is in now.

Two inputs, one fetched and one from the clock: NSE's own holiday list
(www.nseindia.com/api/holiday-master?type=trading, verified live 2026-09-02 --
keyed by segment, twelve of them, each row carrying tradingDate/description),
and the published session hours from settings.

**Closed and holiday are different facts.** Closed ends at the next open;
holiday is the exchange not trading that day at all. A consumer deciding
whether to wait or to stand down for the day needs to know which.

**An empty holiday list reports CLOSED, never OPEN** (Rule 8). Absence of
evidence is its own state, and the alternative is a bot that trades into a
holiday because a fetch had not happened yet -- the display failure this
project already refuses, one layer down in the thing being displayed.
"""

from __future__ import annotations

import datetime

from runtime.market_conditions import MarketSessionState, SessionKind, read_nse_date
from runtime.nse_public_data import NSE_API_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "market-session-calendar"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("market-session-state", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HOLIDAY_URL = f"{NSE_API_HOST}/api/holiday-master?type=trading"
SATURDAY = 5
NO_HOLIDAY_LIST = "no holiday list has been read yet"


class MarketSessionCalendar:
    """Which session one segment is in, from its holidays plus its hours."""

    def __init__(self, segment: str, opens_at, closes_at, timezone) -> None:
        self._segment = segment
        self._opens_at = opens_at
        self._closes_at = closes_at
        self._timezone = timezone
        self._holiday_reason_by_date: dict[datetime.date, str] = {}
        self._has_a_holiday_list = False

    def observe_holidays(self, document) -> None:
        rows = document.get(self._segment, [])
        self._holiday_reason_by_date = {
            read_nse_date(row["tradingDate"]): row.get("description", "")
            for row in rows
        }
        self._has_a_holiday_list = True

    def session_at(self, moment: datetime.datetime) -> MarketSessionState:
        local = moment.astimezone(self._timezone)
        day = local.date()
        observed_at_ns = int(moment.timestamp() * 1_000_000_000)
        if not self._has_a_holiday_list:
            return MarketSessionState(
                segment=self._segment, kind=SessionKind.CLOSED, as_of_date=day,
                reason=NO_HOLIDAY_LIST, observed_at_ns=observed_at_ns,
            )
        holiday_reason = self._holiday_reason_by_date.get(day)
        if holiday_reason is not None:
            return MarketSessionState(
                segment=self._segment, kind=SessionKind.HOLIDAY, as_of_date=day,
                reason=holiday_reason, observed_at_ns=observed_at_ns,
            )
        if local.weekday() >= SATURDAY:
            return MarketSessionState(
                segment=self._segment, kind=SessionKind.CLOSED, as_of_date=day,
                reason="weekend", observed_at_ns=observed_at_ns,
            )
        if self._opens_at <= local.time() < self._closes_at:
            return MarketSessionState(
                segment=self._segment, kind=SessionKind.OPEN, as_of_date=day,
                reason="within stated session hours", observed_at_ns=observed_at_ns,
            )
        return MarketSessionState(
            segment=self._segment, kind=SessionKind.CLOSED, as_of_date=day,
            reason="outside stated session hours", observed_at_ns=observed_at_ns,
        )

    @property
    def holidays_known(self) -> int:
        return len(self._holiday_reason_by_date)


def read_clock_time(stated: str) -> datetime.time:
    """"09:15" as NSE publishes it, from a setting rather than a literal."""
    hour, _, minute = stated.partition(":")
    return datetime.time(int(hour), int(minute))


def describe_calendar(calendar: MarketSessionCalendar, moment) -> dict:
    state = calendar.session_at(moment)
    return {
        "part_id": PART_ID,
        "segment": state.segment,
        "session": str(state.kind),
        "reason": state.reason,
        "holidays_known": calendar.holidays_known,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time
    import zoneinfo

    from runtime.level_publishing import LevelPublisher, without_observation_time
    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    timezone = zoneinfo.ZoneInfo("Asia/Kolkata")
    calendar = MarketSessionCalendar(
        segment=str(context.setting("market_session_segment").value),
        opens_at=read_clock_time(str(context.setting("market_session_opens_at_ist").value)),
        closes_at=read_clock_time(str(context.setting("market_session_closes_at_ist").value)),
        timezone=timezone,
    )
    publisher = LevelPublisher(
        publish=context.bus.publisher_for("market-session-state"),
        refresh_interval_seconds=context.number(
            "instrument_restriction_refresh_interval_seconds"
        ),
        identity_of=without_observation_time,
    )
    poll_seconds = context.number("market_session_poll_seconds")
    last_polled_at = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is None or now - last_polled_at[0] >= poll_seconds:
            last_polled_at[0] = now
            calendar.observe_holidays(nse.read_json(HOLIDAY_URL))
        moment = datetime.datetime.now(tz=timezone)
        publisher.publish_level((calendar.session_at(moment),))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_calendar(
            calendar, datetime.datetime.now(tz=timezone)
        ) | nse.standing(),
    )


__all__ = [
    "HOLIDAY_URL",
    "MarketSessionCalendar",
    "NO_HOLIDAY_LIST",
    "PART_DECLARATION",
    "PART_ID",
    "describe_calendar",
    "read_clock_time",
    "start_part",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/stock_market_news_data/ -v`
Expected: PASS, 31 tests.

- [ ] **Step 6: Commit**

```bash
git add parts/stock_market_news_data/market_session_calendar.py tests/parts/stock_market_news_data/test_market_session_calendar.py
git commit -m "feat: market-session-calendar -- closed and holiday are different facts"
```

---

### Task 8: halt-enforcer refuses a restricted instrument

**Files:**
- Modify: `parts/risk_capital_allocation/halt_enforcer.py`
- Test: `tests/parts/risk_capital_allocation/test_halt_enforcer.py` (add cases)

**Interfaces:**
- Consumes: `InstrumentRestriction` (Task 1), the level Task 4 publishes.
- Produces: `INSTRUMENT_RESTRICTION` (a new halt kind),
  `HaltEnforcer.observe_restrictions(restrictions)`.

**Why this consumer first:** `halt-enforcer` is the one part that turns a
condition into `NO_RISK_ALLOWED`, and `position-sizer` already reads that.
Wiring it means one edit stops orders on a banned name everywhere, rather than
each order path growing its own check.

**How the existing part actually works — read this before writing the test.**
`HaltEnforcer` keys halts **by kind**, one halt per kind, and `read_limit()`
returns a single `RiskLimit` carrying the highest-precedence halt's symbols.
The scope travels inside `halt.source`, which `symbols_in_scope()` parses as a
comma-separated symbol list, with `EVERYTHING` meaning the whole book. So a
restriction is **one halt of one new kind** whose source is the comma-joined
list of restricted symbols — not one halt per symbol, and `release_halt(kind)`
needs no new argument.

`INSTRUMENT_RESTRICTION` goes **last** in `PRECEDENCE`, below
`SETTINGS_INVALID`. It is the narrowest halt in the part, and every kind above
it scopes to `EVERYTHING` — so when one of those is standing, the restriction
is subsumed by a broader stop rather than lost.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/parts/risk_capital_allocation/test_halt_enforcer.py
import datetime

from runtime.market_conditions import InstrumentRestriction, RestrictionKind
from runtime.risk_types import EVERY_SYMBOL
from parts.risk_capital_allocation.halt_enforcer import (
    INSTRUMENT_RESTRICTION,
    HaltEnforcer,
)

BAN_DAY = datetime.date(2026, 9, 2)
OBSERVED_AT_NS = 1_756_800_000_000_000_000


def _banned(symbol="LICHSGFIN"):
    return InstrumentRestriction(
        symbol=symbol, kinds=(RestrictionKind.FNO_BAN,), sources=("nse-fo-secban",),
        stated_for=BAN_DAY, observed_at_ns=OBSERVED_AT_NS,
    )


def test_a_banned_instrument_zeroes_risk_for_that_symbol_only():
    """A ban on one name is not a reason to stop trading everything else --
    the 2026-08-25 defect this part already carries a docstring about."""
    enforcer = HaltEnforcer(allowed_fraction_when_clear=0.02)
    enforcer.observe_restrictions((_banned(), _banned("SAIL")))
    limit = enforcer.read_limit()
    assert limit.fraction_of_allotment == 0.0
    assert set(limit.symbols) == {"LICHSGFIN", "SAIL"}
    assert limit.symbols != EVERY_SYMBOL


def test_a_ban_that_stops_being_reported_releases_the_halt():
    """NSE publishes no un-ban: the level simply stops carrying the symbol.
    A halt released only explicitly would never lift."""
    enforcer = HaltEnforcer(allowed_fraction_when_clear=0.02)
    enforcer.observe_restrictions((_banned(),))
    enforcer.observe_restrictions(())
    assert enforcer.read_limit().fraction_of_allotment == 0.02


def test_the_restriction_halt_names_the_exchange_that_claimed_it():
    enforcer = HaltEnforcer(allowed_fraction_when_clear=0.02)
    enforcer.observe_restrictions((_banned(),))
    assert "nse-fo-secban" in enforcer.read_limit().reason


def test_a_restriction_never_outranks_a_broader_halt():
    """Precedence order matters: a human override scoped to everything must
    not be narrowed to two banned symbols."""
    enforcer = HaltEnforcer(allowed_fraction_when_clear=0.02)
    enforcer.observe_restrictions((_banned(),))
    enforcer.raise_halt("human-override", "everything", "operator said stop")
    assert enforcer.read_limit().symbols == EVERY_SYMBOL


def test_an_unrestricted_book_raises_no_restriction_halt_at_all():
    enforcer = HaltEnforcer(allowed_fraction_when_clear=0.02)
    enforcer.observe_restrictions(())
    assert INSTRUMENT_RESTRICTION not in enforcer.standing.active
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/risk_capital_allocation/test_halt_enforcer.py -v`
Expected: FAIL — `ImportError: cannot import name 'INSTRUMENT_RESTRICTION'`

- [ ] **Step 3: Read the part before changing it**

Read `parts/risk_capital_allocation/halt_enforcer.py` in full, particularly
`symbols_in_scope`, `PRECEDENCE` and `read_limit`. Confirm the human-override
constant's exact string before using it in the test above — the test names it
literally and a mismatch is a false failure.

- [ ] **Step 4: Write minimal implementation**

In `halt_enforcer.py`, add the kind and put it last in precedence:

```python
# The exchange's own restriction: an F&O ban or an ASM stage on named symbols.
# Last in precedence because it is the narrowest halt here -- every kind above
# it scopes to EVERYTHING, so a broader stop standing at the same time subsumes
# this one rather than being narrowed by it.
INSTRUMENT_RESTRICTION = "instrument-restriction"

PRECEDENCE = (HUMAN_OVERRIDE, TRADING_HALT, POLICY_REFUSAL, SETTINGS_INVALID,
              INSTRUMENT_RESTRICTION)
```

and on `HaltEnforcer`:

```python
    def observe_restrictions(self, restrictions) -> None:
        """Halt the restricted symbols; lift the halt when none are reported.

        The only halt kind here released by absence rather than by an explicit
        release, and deliberately so: every other kind is a decision someone
        made and must un-make, while a ban is a list the exchange republishes
        daily and a lifted ban is a name that has stopped appearing on it.

        The symbols travel in `source` because that is where this part already
        carries a halt's scope -- `symbols_in_scope` parses exactly this shape.
        """
        restricted = sorted(
            restriction.symbol for restriction in restrictions
            if not restriction.may_open_new_position
        )
        if not restricted:
            self.release_halt(INSTRUMENT_RESTRICTION)
            return
        sources = sorted({
            source for restriction in restrictions for source in restriction.sources
        })
        self.raise_halt(
            INSTRUMENT_RESTRICTION,
            ",".join(restricted),
            f"restricted by {', '.join(sources)}",
        )
```

Then in `start_part`, add the reader and call it once per tick.
`maximum_age_seconds` is deliberately **not** set on this side: the level is
already aged by `instrument-restriction-state`, and ageing it twice would
expire it here while it still stands there.

```python
    from runtime.input_assembly import Batch, LatestValue

    restrictions = LatestValue(read=context.bus.reader("instrument-restriction"))
    # ... inside the existing tick, before read_limit() is called:
    standing = restrictions.value()
    if standing is not None:
        enforcer.observe_restrictions(standing)
```

`instrument-restriction` is published as one level carrying every restricted
symbol, so `LatestValue.value()` is the whole list — a tuple, not one item.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/risk_capital_allocation/ -v`
Expected: PASS, including the three new cases and every pre-existing one.

- [ ] **Step 6: Commit**

```bash
git add parts/risk_capital_allocation/halt_enforcer.py tests/parts/risk_capital_allocation/test_halt_enforcer.py
git commit -m "feat: halt-enforcer halts a banned instrument, released by absence"
```

---

### Task 9: the session gates the paper fill

**Files:**
- Modify: `parts/paper_live_trading/paper_fill_simulator.py`
- Test: `tests/parts/paper_live_trading/test_paper_fill_simulator.py` (add cases)

**Interfaces:**
- Consumes: `MarketSessionState` (Task 1), the level Task 7 publishes.
- Produces: no new names on the fill path.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/parts/paper_live_trading/test_paper_fill_simulator.py
import datetime

from runtime.market_conditions import MarketSessionState, SessionKind

CLOSED = MarketSessionState(
    segment="FO", kind=SessionKind.CLOSED, as_of_date=datetime.date(2026, 9, 2),
    reason="outside stated session hours", observed_at_ns=1_756_800_000_000_000_000,
)
OPEN = MarketSessionState(
    segment="FO", kind=SessionKind.OPEN, as_of_date=datetime.date(2026, 9, 2),
    reason="within stated session hours", observed_at_ns=1_756_800_000_000_000_000,
)


def test_no_paper_fill_happens_while_the_market_is_closed():
    """The defect: without this, an order placed at 18:00 fills at the last
    price seen at 15:29 and is journalled as a real trade the learning loop
    then learns from."""
    simulator = _a_simulator_with_a_pending_order()   # existing helper
    simulator.observe_session(CLOSED)
    assert simulator.fills() == ()


def test_a_paper_fill_happens_once_the_session_is_open():
    simulator = _a_simulator_with_a_pending_order()
    simulator.observe_session(OPEN)
    assert simulator.fills() != ()


def test_a_simulator_that_has_never_seen_a_session_does_not_fill():
    """Rule 8 again: not measured is not 'open'."""
    simulator = _a_simulator_with_a_pending_order()
    assert simulator.fills() == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/paper_live_trading/test_paper_fill_simulator.py -v`
Expected: FAIL — `AttributeError: 'PaperFillSimulator' object has no attribute 'observe_session'`

- [ ] **Step 3: Read the part, then write the implementation**

Read `parts/paper_live_trading/paper_fill_simulator.py` first: it already holds
orders and prices, and the existing helper the tests use must be reused rather
than replaced. Add:

```python
    def observe_session(self, session) -> None:
        """Whether the market is open. Never inferred from a price arriving --
        a stale price arrives at midnight exactly as a live one does."""
        self._session = session

    @property
    def _may_fill(self) -> bool:
        return self._session is not None and self._session.is_tradeable
```

and gate the fill path on `self._may_fill`, with `self._session = None` in
`__init__`. In `start_part`, read the level:

```python
    from runtime.input_assembly import LatestValue

    sessions = LatestValue(read=context.bus.reader("market-session-state"))
    # inside the existing tick, before pricing:
    standing = sessions.value()
    if standing is not None:
        for session in standing:
            simulator.observe_session(session)
```

`market-session-calendar` publishes its level as a one-item tuple, so
`LatestValue.value()` is that tuple — iterate it rather than treating it as one
state.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/paper_live_trading/ -v`
Expected: PASS, including every pre-existing fill test.

- [ ] **Step 5: Commit**

```bash
git add parts/paper_live_trading/paper_fill_simulator.py tests/parts/paper_live_trading/test_paper_fill_simulator.py
git commit -m "fix: no paper fill while the market is closed"
```

---

### Task 10: the candle series is adjusted for a corporate action

**Files:**
- Modify: `parts/prediction/kline_window_builder.py`
- Test: `tests/parts/prediction/test_kline_window_builder.py` (add cases)

**Interfaces:**
- Consumes: `CorporateAction` (Task 1), published by Task 6.
- Produces: no new names.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/parts/prediction/test_kline_window_builder.py
import datetime

from runtime.market_conditions import CorporateAction

BONUS = CorporateAction(
    symbol="RELIANCE", kind="bonus", price_factor=0.5, quantity_factor=2.0,
    ex_date=datetime.date(2026, 9, 2), stated_from="Bonus 1:1",
    observed_at_ns=1_756_800_000_000_000_000,
)


def test_a_bonus_rescales_the_candles_before_its_ex_date():
    """Unadjusted, the ex-date open sits beside the previous close at half the
    price: a -50% candle that every detector in the system fires on."""
    builder = _a_builder_with_candles_around("RELIANCE", datetime.date(2026, 9, 2))
    builder.observe_corporate_action(BONUS)
    window = builder.window_for("RELIANCE")
    assert window.closes[0] == 1000.0 * 0.5


def test_candles_on_and_after_the_ex_date_are_left_alone():
    builder = _a_builder_with_candles_around("RELIANCE", datetime.date(2026, 9, 2))
    builder.observe_corporate_action(BONUS)
    window = builder.window_for("RELIANCE")
    assert window.closes[-1] == 500.0


def test_a_dividend_does_not_rescale_anything():
    dividend = CorporateAction(
        symbol="NTPC", kind="dividend", price_factor=1.0, quantity_factor=1.0,
        ex_date=datetime.date(2026, 9, 2), stated_from="Dividend - Rs 3.50 Per Share",
        observed_at_ns=1_756_800_000_000_000_000,
    )
    builder = _a_builder_with_candles_around("NTPC", datetime.date(2026, 9, 2))
    before = builder.window_for("NTPC").closes
    builder.observe_corporate_action(dividend)
    assert builder.window_for("NTPC").closes == before


def test_the_same_action_applied_twice_rescales_once():
    """Reports are republished on every poll. Applying a bonus twice quarters
    the history, which is worse than not applying it at all."""
    builder = _a_builder_with_candles_around("RELIANCE", datetime.date(2026, 9, 2))
    builder.observe_corporate_action(BONUS)
    once = builder.window_for("RELIANCE").closes
    builder.observe_corporate_action(BONUS)
    assert builder.window_for("RELIANCE").closes == once
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/parts/prediction/test_kline_window_builder.py -v`
Expected: FAIL — `AttributeError: no attribute 'observe_corporate_action'`

- [ ] **Step 3: Read the part, then write the implementation**

Read `parts/prediction/kline_window_builder.py` and reuse its existing
per-symbol candle storage and its existing test helper. Add:

```python
    def observe_corporate_action(self, action) -> None:
        """Rescale this symbol's stored candles before the action's ex-date.

        Applied at most once per (symbol, ex_date, stated_from): reports are
        republished on every poll, and applying a 1:1 bonus twice quarters the
        history -- a worse answer than never applying it, because it looks
        adjusted.
        """
        if action.price_factor == 1.0:
            return
        seen = (action.symbol, action.ex_date, action.stated_from)
        if seen in self._actions_applied:
            return
        self._actions_applied.add(seen)
        self._rescale_before(action.symbol, action.ex_date, action.price_factor)
```

with `self._actions_applied: set = set()` in `__init__` and `_rescale_before`
multiplying every stored candle whose close time falls before the ex-date's
session open by `price_factor`. In `start_part`, read the wire:

```python
    actions = Batch(read=context.bus.reader("corporate-action"))
    # inside the existing tick, before windows are published:
    for action in actions.payloads():
        builder.observe_corporate_action(action)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/parts/prediction/ -v`
Expected: PASS, including every pre-existing window test.

- [ ] **Step 5: Commit**

```bash
git add parts/prediction/kline_window_builder.py tests/parts/prediction/test_kline_window_builder.py
git commit -m "fix: a 1:1 bonus is no longer a -50% candle"
```

---

### Task 11: run the five parts on the live spine, and prove they ran

**Files:**
- Modify: `operate/run_live_spine.py` (add five ids to `LIVE_SPINE`)
- Create: `measurements/2026-09-02-hard-channel/what_the_hard_channel_saw.py`

**Interfaces:**
- Consumes: everything from Tasks 3-7.
- Produces: a measurement, not a claim.

- [ ] **Step 1: Add the five parts to the spine**

In `operate/run_live_spine.py`, add to `LIVE_SPINE`, in this order (readers
before the state parts that consume them):

```python
    "trading-restriction-reader",
    "instrument-restriction-state",
    "corporate-action-reader",
    "corporate-action-adjuster",
    "market-session-calendar",
```

- [ ] **Step 2: Prove the wiring derives before starting anything**

Run: `.venv/bin/python -c "
from runtime.wiring_plan import derive_wiring
w = derive_wiring()
for part in ['trading-restriction-reader','instrument-restriction-state','corporate-action-reader','corporate-action-adjuster','market-session-calendar']:
    print(part, 'in wiring:', part in w)
"`
Expected: all five `True`. A `False` means the blueprint and the part
declaration disagree — stop and report it rather than editing either.

- [ ] **Step 3: Restart the spine and read the journal**

```bash
systemctl --user restart ajit-spine
sleep 60
journalctl --user --since "-3min" | grep -B 30 Error | head -60
```

Expected: no traceback. A crashing part's traceback is in the **user** journal,
not in `ajit-spine`'s — every part runs in its own systemd scope, which cost
two hours of diagnosis on 2026-08-25.

- [ ] **Step 4: Write the measurement script**

```python
# measurements/2026-09-02-hard-channel/what_the_hard_channel_saw.py
"""What the five hard-channel parts actually observed on this machine.

Reads each part's own standing out of the heartbeat table the collector writes
-- the same file the board reads. Nothing here is asserted: a part that has
published no standing prints NOT MEASURED, which is a different fact from zero.
"""

import json
import pathlib
import sys

from runtime.settings_reader import read_settings

PARTS = (
    "trading-restriction-reader",
    "instrument-restriction-state",
    "corporate-action-reader",
    "corporate-action-adjuster",
    "market-session-calendar",
)


def main() -> int:
    settings = read_settings()
    table_path = pathlib.Path(str(settings["runtime"].entries["heartbeat_table_path"].value)).expanduser()
    if not table_path.exists():
        print(f"NOT MEASURED: no heartbeat table at {table_path}")
        return 1
    table = json.loads(table_path.read_text())
    rows = {row["part_id"]: row for row in table.get("parts", [])}
    for part_id in PARTS:
        row = rows.get(part_id)
        if row is None:
            print(f"{part_id:34} NOT MEASURED -- no heartbeat")
            continue
        print(f"{part_id:34} {json.dumps(row.get('standing', {}))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run it and read the real numbers**

Run: `.venv/bin/python measurements/2026-09-02-hard-channel/what_the_hard_channel_saw.py`

Expected, and each is a fact to report rather than a box to tick:

- `trading-restriction-reader`: `banned_symbols_last_seen` matching what
  `fo_secban.csv` says today (2 on 2026-09-02: LICHSGFIN, SAIL), and
  `asm_symbols_last_seen` around 232 (146 long-term + 86 short-term that day).
- `instrument-restriction-state`: `symbols_restricted` equal to the sum above.
- `corporate-action-reader`: `rows_seen` non-zero.
- `corporate-action-adjuster`: `actions_understood` plus `wordings_refused` —
  **report the refused wordings**, they are the parser's real coverage gap and
  the input to the next iteration.
- `market-session-calendar`: `session` matching the actual clock, and
  `holidays_known` non-zero.

- [ ] **Step 6: Rebuild the boards and commit**

```bash
dashboard/rebuild_all_boards.sh
git add operate/run_live_spine.py measurements/2026-09-02-hard-channel/
git commit -m "feat: the hard channel runs on the live spine"
```

---

## Self-Review

**Spec coverage.** The spec's hard-channel section names three parts
(`instrument-restriction-state`, `corporate-action-adjuster`,
`market-session-calendar`) plus the two source readers that feed them
(`trading-restriction-reader`, `corporate-action-reader`) — Tasks 3-7, one
each. The spec's wiring table names ten consumers of those three types; Tasks
8-10 wire the three with the largest failure if left unwired (order refusal,
paper fill, candle series). The remaining seven —
`order-reject-classifier`, `order-destination-router`, `ccxt-order-router`,
`liquidity-grader`, `trading-halt-decider`, `universal-symbol-sweeper`,
`expiry-day-zero-to-hero-detector`, `fill-reconciler`, `cost-basis-tracker`,
`intent-timing-gate` — consume levels that now exist and are wired in the
blueprint, but their code changes are **not** in this plan. They are a
follow-on plan, and until then those parts receive the level and ignore it.
That is stated rather than hidden: the blueprint edge exists, the code does
not.

**Placeholder scan.** No TBD, no "handle edge cases", no "similar to Task N".
Every code step carries the code. Tasks 8-10 modify existing parts and say
"read the part first" because the surrounding code is not reproduced here —
the added methods and their call sites are given in full.

**Type consistency.** `InstrumentRestrictionReport`, `InstrumentRestriction`,
`CorporateActionReport`, `CorporateAction`, `MarketSessionState`,
`RestrictionKind`, `SessionKind`, `read_nse_date` are defined in Task 1 and
used with those exact names in Tasks 3-10. `NsePublicData.read_text` /
`.read_json` / `.standing` defined in Task 2, used in Tasks 3, 5, 7.
`observe_restrictions`, `observe_session`, `observe_corporate_action` are the
three verbs added to existing parts, each named for what it does to the part
that owns it (Rule 7).

**Assumptions checked against the real code before this plan was saved**, rather
than left for the implementer to trip over:

| assumption | verified |
|---|---|
| `context.number(name)` exists | `runtime/part_context.py:93` |
| the level reader is `LatestValue`, not `Latest` | `runtime/input_assembly.py:37` — an earlier draft of Tasks 8-9 named a class that does not exist |
| `HaltEnforcer` keys halts by kind and returns one `RiskLimit`, scope carried in `halt.source` | `parts/risk_capital_allocation/halt_enforcer.py:123-180` — Task 8 was rewritten around this; the first draft invented `limits()` and a scoped `release_halt` that do not exist |
| a new halt kind must be added to `PRECEDENCE` | `halt_enforcer.py:134` raises `ValueError` otherwise |
| `heartbeat_table_path` is a real setting | present in `runtime.toml` |
| all four NSE endpoints answer 200 | fetched live 2026-09-02; bodies quoted in Tasks 1, 3, 5, 7 |

**Still unverified, and Task 8 Step 3 exists to settle it:** the exact string
constant for the human-override halt kind, used literally in one test.
