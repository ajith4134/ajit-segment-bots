# Upstox Broker Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four `broker-adapter` parts declared in `docs/features.json` (2026-09-01) — token refresh, instrument catalogue, market feed, tape write — against a fresh `BrokerAdapter` contract, with Upstox as the first implementation.

**Architecture:** A broker-agnostic `BrokerAdapter` ABC (`runtime/brokers/broker_adapter.py`, patterned on the retired-for-crypto `runtime/venues/venue_adapter.py` but shaped for NSE/BSE instruments — instrument keys, lot size, strike/expiry, no funding/premium). `runtime/brokers/upstox.py` implements it against Upstox's real, verified API surface: a daily gzipped instrument-master file, a protobuf-encoded WebSocket feed (schema fetched and committed, not assumed), and TOTP-based unattended daily auth via the `upstox-totp` package. Four parts follow this codebase's existing part shape exactly (`PartDeclaration` + `start_part(context)` + `run_part`), read-side only — no order placement in this plan.

**Tech Stack:** Python 3.14 (project standard, D-011), `protobuf==7.36.1` (new), `upstox-totp==1.0.8` (new, pinned per spec), `grpcio-tools==1.83.1` (codegen-only, not a runtime dependency).

**Spec:** `docs/superpowers/specs/2026-09-01-upstox-adapter-design.md` (design, with the auth/secrets addendum) and `docs/proposals/upstox-broker-adapter.md` (what's declared and why). This plan implements exactly the four parts + one category those two documents already committed to — no scope beyond them.

## Global Constraints

- **Python 3.14.4, standard CPython build** (D-011) — no free-threaded build, no source compilation; every new dependency must ship a wheel for this interpreter (verified in Task 1/3 below).
- **Every dependency pinned exact, with a written reason** (RL-065) — added to `pyproject.toml`'s `dependencies` list in the same style as the existing four entries (comment above each, dated).
- **No numeric literals in decision code** (RL-061) — every threshold, interval, or count that a part acts on is a named setting in `~/.config/ajit-segment-bots/settings/runtime.toml` with a provenance note (who, when, why this number), never a bare constant in the part's own file, except where the number is a structural fact about the protocol itself (e.g. Upstox's documented connection cap of 2) — those are named module constants with a comment citing the source, the same way `MAXIMUM_CATALOGUE_PAGES` is in `symbol_catalogue_reader.py`.
- **Every part is `PartDeclaration` + `start_part(context)` + `run_part`** (T-1, T-2, T-3) — no part invents its own lifecycle. `off` is the harness killing the process; nothing in part code implements "off" itself.
- **A part names data, never another part** (R-01/T-4) — every cross-part dependency is a `consumes`/`produces` data type from `docs/features.json`'s `broker-adapter` category, resolved through `context.bus`, never an import of another part's module.
- **`runtime/venues/venue_adapter.py` is untouched.** 332 crypto parts still import it; retiring crypto is a separate, much larger piece of work nobody has asked for yet. `runtime/brokers/` is a new, non-overlapping package — it imports nothing from `runtime/venues/`.
- **Tests run on real data, not invented fixtures** (RL-063), with one honest caveat stated up front: unlike the crypto build, no live Upstox account is connected yet (secrets are still placeholders, `docs/secrets.md`), so there is no captured tape to test against. The plan uses the two closest things to real data available: Upstox's own documented sample payloads (fetched directly from their developer docs 2026-09-01, cited by URL in the test file) for JSON-shaped responses, and messages built with the *actual* generated protobuf classes from Upstox's *actual* committed `.proto` schema (never hand-rolled bytes) for the feed decoder — real wire format, test-chosen field values. Each test says which of the two it is.
- **`RequestMode` correction versus the spec:** the spec's §5 used `"full"` for the richest non-Plus mode, copied from the docs page's illustrative JSON. The actual `.proto` enum (fetched 2026-09-01, `assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto`) names it `full_d5`. This plan uses `full_d5` everywhere; if a live test later shows the subscribe request itself expects the string `"full"` instead, that's a live-verification finding for whoever first runs this against a real token, not a guess to bake in now.

---

## File Structure

| File | Responsibility |
|---|---|
| `runtime/brokers/__init__.py` | empty, marks the package |
| `runtime/brokers/broker_adapter.py` | the contract: data shapes + `BrokerAdapter` ABC, broker-agnostic |
| `runtime/brokers/upstox_market_data_feed.proto` | Upstox's own schema, committed verbatim, with its source URL and fetch date in a header comment |
| `runtime/brokers/upstox_market_data_feed_pb2.py` | generated from the above via `grpcio-tools`, committed (no protoc needed at install time) |
| `runtime/brokers/upstox.py` | `UpstoxAdapter(BrokerAdapter)` — everything Upstox-specific |
| `runtime/tape.py` (modified) | two new `StreamKind` members, one new `TradeFidelity` member — additive only |
| `parts/broker_adapter/broker_token_refresh_scheduler.py` | Task 3 |
| `parts/broker_adapter/broker_instrument_catalogue_reader.py` | Task 4 |
| `parts/broker_adapter/broker_market_feed_reader.py` | Task 5 |
| `parts/broker_adapter/broker_market_tape_writer.py` | Task 6 |
| `tests/runtime/brokers/test_broker_adapter.py` | contract-level tests (fact validation, conformance tuple) |
| `tests/runtime/brokers/test_upstox.py` | `UpstoxAdapter` tests |
| `tests/parts/broker_adapter/test_*.py` | one per part |

---

### Task 1: `runtime/tape.py` extension + the `BrokerAdapter` contract

**Files:**
- Modify: `runtime/tape.py` (`StreamKind` enum, `TradeFidelity` enum)
- Create: `runtime/brokers/__init__.py`
- Create: `runtime/brokers/broker_adapter.py`
- Test: `tests/runtime/brokers/test_broker_adapter.py`
- Test: `tests/runtime/test_tape.py` (add cases; file already exists — check with `ls tests/runtime/test_tape.py` before writing, extend rather than replace)

**Interfaces:**
- Produces (used by every later task):
  - `runtime.tape.StreamKind.OPEN_INTEREST` (value `6`), `runtime.tape.StreamKind.OPTION_GREEKS` (value `7`)
  - `runtime.tape.TradeFidelity.LAST_TRADED_PRICE_ONLY` (value `"last-traded-price-only"`)
  - `runtime.brokers.broker_adapter.BrokerFact(name, value, unit, source)`
  - `runtime.brokers.broker_adapter.BrokerFactWithoutSource` (exception)
  - `runtime.brokers.broker_adapter.TokenExpiryPolicy` (StrEnum: `NEVER`, `DAILY_AT_FIXED_TIME`)
  - `runtime.brokers.broker_adapter.BrokerTokenPolicy(expiry, daily_expiry_time_ist, auto_refreshable, source)`
  - `runtime.brokers.broker_adapter.InstrumentListing(instrument_key, exchange, segment, instrument_type, trading_symbol, lot_size, tick_size, freeze_quantity, expiry_ms, strike_price, underlying_key, intraday_margin_percent, intraday_leverage)`
  - `runtime.brokers.broker_adapter.SubscriptionMode` (StrEnum: `LTPC="ltpc"`, `OPTION_GREEKS="option_greeks"`, `FULL="full_d5"`, `FULL_D30="full_d30"`)
  - `runtime.brokers.broker_adapter.SubscriptionRequest(instrument_key, mode)`
  - `runtime.brokers.broker_adapter.LtpUpdate(instrument_key, last_traded_price, last_traded_quantity, last_traded_time_ms, close_price, broker_time_ns)`
  - `runtime.brokers.broker_adapter.BrokerCandle(instrument_key, interval, open, high, low, close, volume, bar_time_ms, is_closed)` — `is_closed: bool | None`, `None` when the broker doesn't state it (Upstox never does — see Task 2)
  - `runtime.brokers.broker_adapter.BrokerOrderBookLevel(bid_price, bid_quantity, ask_price, ask_quantity)`
  - `runtime.brokers.broker_adapter.BrokerOrderBookUpdate(instrument_key, levels, broker_time_ns)` — `levels: tuple[BrokerOrderBookLevel, ...]`
  - `runtime.brokers.broker_adapter.BrokerOpenInterest(instrument_key, open_interest, volume_traded_today, total_buy_quantity, total_sell_quantity, average_traded_price, broker_time_ns)`
  - `runtime.brokers.broker_adapter.BrokerOptionGreeks(instrument_key, delta, theta, gamma, vega, rho, implied_volatility, broker_time_ns)`
  - `runtime.brokers.broker_adapter.DecodedFeedMessage(kind, market_segment_status, ltp_updates, candles, book_updates, open_interest, option_greeks, broker_time_ns)` — `kind: str` one of `"market_info"`, `"initial_feed"`, `"live_feed"` (matches the proto `Type` enum's own three names)
  - `runtime.brokers.broker_adapter.BanSignal`, `HeartbeatDiscipline`, `ConnectionDiscipline` — same field shapes as `runtime/venues/venue_adapter.py`'s, redefined locally (not imported — Global Constraints)
  - `runtime.brokers.broker_adapter.BrokerAdapter` (ABC) with the methods listed in Step 7 below
  - `runtime.brokers.broker_adapter.QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA`, `QUESTIONS_ANSWERED_FROM_BROKER_DATA` — conformance tuples, same pattern as `venue_adapter.py`'s

- [ ] **Step 1: Check the existing tape test file, then write the failing test for the two new `StreamKind` values**

```bash
ls tests/runtime/test_tape.py
grep -n "class TestStreamKind\|def test_stream_kind" tests/runtime/test_tape.py
```

Add to that file (or create it if it genuinely doesn't exist — check first):

```python
def test_stream_kind_gains_open_interest_and_option_greeks_without_renumbering_existing():
    from runtime.tape import StreamKind

    # Existing five must be unchanged -- a tape written before this change
    # reads back identically, since StreamKind is stored as one byte (T-5).
    assert StreamKind.TRADE == 1
    assert StreamKind.CANDLE == 2
    assert StreamKind.BOOK == 3
    assert StreamKind.QUOTE == 4
    assert StreamKind.PREMIUM == 5
    assert StreamKind.OPEN_INTEREST == 6
    assert StreamKind.OPTION_GREEKS == 7


def test_trade_fidelity_gains_last_traded_price_only():
    from runtime.tape import TradeFidelity

    assert TradeFidelity.EVERY_PRINT == "every-print"
    assert TradeFidelity.VENUE_AGGREGATED == "venue-aggregated"
    assert TradeFidelity.LAST_TRADED_PRICE_ONLY == "last-traded-price-only"
```

- [ ] **Step 2: Run it, confirm it fails**

```bash
.venv/bin/python3 -m pytest tests/runtime/test_tape.py -k "open_interest or last_traded_price_only" -v
```
Expected: FAIL — `AttributeError: OPEN_INTEREST`, `AttributeError: LAST_TRADED_PRICE_ONLY`.

- [ ] **Step 3: Extend the two enums in `runtime/tape.py`**

Find the `StreamKind` class (currently ends at `PREMIUM = 5`) and add, preserving every existing line:

```python
    # Open interest and today's traded volume/buy-sell quantity for one
    # derivative contract. Added 2026-09-01 for the Indian-markets broker
    # adapter -- no crypto perpetual venue ever published this (spot/perp
    # pricing has no concept of open interest the way a listed derivative
    # contract does). Appended, never inserted, so a tape written before this
    # change reads back with every existing value unchanged.
    OPEN_INTEREST = 6
    # Delta, theta, gamma, vega, rho and implied volatility for one options
    # contract. Added 2026-09-01, same reasoning as OPEN_INTEREST above.
    OPTION_GREEKS = 7
```

Find `TradeFidelity` (currently `EVERY_PRINT` / `VENUE_AGGREGATED`) and add:

```python
    # A retail broker's last-traded-price ticker: the exchange's own last
    # print, restated on update, not a stream of every individual print the
    # way a crypto venue's trade feed is. Added 2026-09-01 -- Upstox's feed
    # has no per-print trade stream at all (spec section 5); recording an LTP
    # update as EVERY_PRINT or VENUE_AGGREGATED would claim a resolution this
    # feed does not have, the same reasoning the other two values exist for.
    LAST_TRADED_PRICE_ONLY = "last-traded-price-only"
```

- [ ] **Step 4: Run it, confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/test_tape.py -k "open_interest or last_traded_price_only" -v
```
Expected: PASS.

- [ ] **Step 5: Run the FULL tape test suite to confirm nothing existing broke**

```bash
.venv/bin/python3 -m pytest tests/runtime/test_tape.py -v
```
Expected: every prior test still PASSes — this step exists because `StreamKind`/`TradeFidelity` are shared substrate 332 crypto parts already depend on; a purely-additive change must prove it stayed purely additive.

- [ ] **Step 6: Commit**

```bash
git add runtime/tape.py tests/runtime/test_tape.py
git commit -m "feat: extend StreamKind and TradeFidelity for the broker-adapter feed

OPEN_INTEREST and OPTION_GREEKS have no crypto equivalent; LAST_TRADED_PRICE_ONLY
distinguishes a retail LTP ticker from a real print stream. All additive --
existing values and their byte encoding are unchanged."
```

- [ ] **Step 7: Write the failing test for `BrokerFact`'s source requirement**

```python
# tests/runtime/brokers/test_broker_adapter.py
import pytest

from runtime.brokers.broker_adapter import BrokerFact, BrokerFactWithoutSource


def test_broker_fact_refuses_construction_without_a_source():
    with pytest.raises(BrokerFactWithoutSource):
        BrokerFact(name="connections_per_user", value=2, unit="count", source="")


def test_broker_fact_accepts_a_real_source():
    fact = BrokerFact(
        name="connections_per_user", value=2, unit="count",
        source="upstox v3 market-data-feed docs, fetched 2026-09-01",
    )
    assert fact.value == 2
```

- [ ] **Step 8: Run it, confirm it fails** (`ModuleNotFoundError: No module named 'runtime.brokers'`)

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_broker_adapter.py -v
```

- [ ] **Step 9: Create the package and the contract's data shapes + `BrokerFact`**

```python
# runtime/brokers/__init__.py
```
(empty)

```python
# runtime/brokers/broker_adapter.py
"""The shape every Indian-market broker adapter is, and nothing else has to know.

Fresh contract, not an extension of runtime/venues/venue_adapter.py -- that one
is shaped for crypto perpetuals (funding, mark price, premium) and none of it
exists for NSE/BSE cash and derivatives. Retiring it is a separate piece of
work; this module imports nothing from it (docs/superpowers/specs/
2026-09-01-upstox-adapter-design.md section 1).

Upstox is the first of six brokers docs/goal.md commits to. Every method here
is broker-agnostic on purpose -- which broker answers is a settings choice
resolved through an adapter registry, never a part's identity (T-1, T-4), the
same discipline venue_adapter.py already used for its two crypto venues.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Mapping, Sequence


class BrokerFactWithoutSource(ValueError):
    """A broker limit was declared without saying where it was read from."""


@dataclass(frozen=True)
class BrokerFact:
    """One number the broker itself fixes, and where that number was read.

    Same shape and same reasoning as venue_adapter.VenueFact (RL-061): the
    operator owns settings, the broker owns these, and both carry provenance.
    """

    name: str
    value: int | float | str | tuple[int | str, ...]
    unit: str
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise BrokerFactWithoutSource(
                f"broker fact '{self.name}' carries no source. A capacity figure a "
                f"part acts on must say which document or measurement it came "
                f"from -- RL-061 does not stop at the broker boundary."
            )


class TokenExpiryPolicy(enum.StrEnum):
    """What kind of session expiry a broker imposes -- T-5, an explicit set.

    Crypto had no equivalent: an API key signs each request and does not
    expire on its own schedule. A broker session here can, so this is the one
    genuinely new concept versus venue_adapter's contract.
    """

    NEVER = "never"
    DAILY_AT_FIXED_TIME = "daily-at-fixed-time"


@dataclass(frozen=True)
class BrokerTokenPolicy:
    """How this broker's session token expires, and whether refresh can be automated.

    `daily_expiry_time_ist` is only meaningful under DAILY_AT_FIXED_TIME.
    `auto_refreshable` says whether a scheduler can renew the token without a
    human -- Upstox's documented flows all need one, but TOTP-based headless
    login (spec section 3) makes it true in practice, which is a fact worth
    a part being able to read rather than assume.
    """

    expiry: TokenExpiryPolicy
    daily_expiry_time_ist: object | None  # datetime.time, kept loosely typed to avoid a stdlib import here
    auto_refreshable: bool
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise BrokerFactWithoutSource(
                f"a token expiry policy carries no source -- when a session dies is "
                f"a fact a scheduler acts on, the same as any other broker fact."
            )


@dataclass(frozen=True)
class InstrumentListing:
    """One contract as a broker's own instrument master lists it.

    Deliberately not venue_adapter.SymbolListing -- the fields genuinely
    differ (instrument_key, lot_size, strike/expiry/option-type) and reusing
    that type id would wire this reader into every existing crypto consumer
    of symbol-universe (docs/proposals/upstox-broker-adapter.md).

    `expiry_ms`, `strike_price`, `underlying_key` are None for a plain equity
    listing (Upstox's own JSON omits them there, not zeroes them -- absence is
    carried as absence, never filled in).
    """

    instrument_key: str
    exchange: str
    segment: str
    instrument_type: str
    trading_symbol: str
    lot_size: int | None
    tick_size: float | None
    freeze_quantity: float | None
    expiry_ms: int | None
    strike_price: float | None
    underlying_key: str | None
    # Only present in Upstox's MIS-specific instrument file, per symbol. None
    # for a listing read from a file that doesn't carry it, never zero.
    intraday_margin_percent: float | None
    intraday_leverage: float | None


class SubscriptionMode(enum.StrEnum):
    """The four ways to ask Upstox's v3 feed for one instrument's data.

    Values match the .proto's own RequestMode enum names exactly (Global
    Constraints: FULL is "full_d5", not "full" -- corrected from the spec's
    docs-page reading against the actual wire schema).
    """

    LTPC = "ltpc"
    OPTION_GREEKS = "option_greeks"
    FULL = "full_d5"
    FULL_D30 = "full_d30"


@dataclass(frozen=True)
class SubscriptionRequest:
    """One instrument, at one mode, that a connection should carry."""

    instrument_key: str
    mode: SubscriptionMode


@dataclass(frozen=True)
class LtpUpdate:
    """One last-traded-price update. See TradeFidelity.LAST_TRADED_PRICE_ONLY --
    this is never a print, it is the exchange's own last-traded-price ticker."""

    instrument_key: str
    last_traded_price: float
    last_traded_quantity: float | None
    last_traded_time_ms: int
    close_price: float | None
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerCandle:
    """One OHLC bar. `is_closed` is None when the broker does not say --
    Upstox never does (Task 2); a reader that guessed would be stating a fact
    the broker itself never stated."""

    instrument_key: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_time_ms: int
    is_closed: bool | None


@dataclass(frozen=True)
class BrokerOrderBookLevel:
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float


@dataclass(frozen=True)
class BrokerOrderBookUpdate:
    instrument_key: str
    levels: tuple[BrokerOrderBookLevel, ...]
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerOpenInterest:
    instrument_key: str
    open_interest: float
    volume_traded_today: float
    total_buy_quantity: float
    total_sell_quantity: float
    average_traded_price: float
    broker_time_ns: int


@dataclass(frozen=True)
class BrokerOptionGreeks:
    instrument_key: str
    delta: float
    theta: float
    gamma: float
    vega: float
    rho: float
    implied_volatility: float
    broker_time_ns: int


@dataclass(frozen=True)
class DecodedFeedMessage:
    """One decoded WebSocket message, decomposed into this project's own
    record kinds. `kind` matches the proto Type enum's own names, so a reader
    never has to translate an integer back into what it means.

    Every tuple is empty rather than None when this message carried none of
    that kind -- a market_info message has empty everything except
    market_segment_status, for instance (spec section 5)."""

    kind: str  # "market_info" | "initial_feed" | "live_feed"
    market_segment_status: Mapping[str, str] | None
    ltp_updates: tuple[LtpUpdate, ...]
    candles: tuple[BrokerCandle, ...]
    book_updates: tuple[BrokerOrderBookUpdate, ...]
    open_interest: tuple[BrokerOpenInterest, ...]
    option_greeks: tuple[BrokerOptionGreeks, ...]
    broker_time_ns: int


@dataclass(frozen=True)
class BanSignal:
    broker_id: str
    observed_code: str
    reason: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class HeartbeatDiscipline:
    expects_client_ping: bool
    interval_seconds: float | None = None
    ping_frame: bytes | None = None


@dataclass(frozen=True)
class ConnectionDiscipline:
    lifetime_seconds: float | None = None
    new_connections_per_window: int | None = None
    rate_window_seconds: float | None = None
    concurrent_connections: int | None = None
```

- [ ] **Step 10: Run Step 7's test, confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_broker_adapter.py -v
```
Expected: PASS (both tests).

- [ ] **Step 11: Write the failing test for the `BrokerAdapter` ABC's conformance tuples**

```python
def test_broker_adapter_conformance_tuples_name_real_methods():
    from runtime.brokers.broker_adapter import (
        BrokerAdapter,
        QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA,
        QUESTIONS_ANSWERED_FROM_BROKER_DATA,
    )

    every_question = QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA + QUESTIONS_ANSWERED_FROM_BROKER_DATA
    for name in every_question:
        assert hasattr(BrokerAdapter, name), f"{name} is not a method on BrokerAdapter"
    # No overlap: a question needs live data or it doesn't, never both lists.
    assert not set(QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA) & set(QUESTIONS_ANSWERED_FROM_BROKER_DATA)
```

- [ ] **Step 12: Run it, confirm it fails** (`BrokerAdapter` doesn't exist yet)

- [ ] **Step 13: Add the `BrokerAdapter` ABC to the same file**

Append to `runtime/brokers/broker_adapter.py`:

```python
class BrokerAdapter(abc.ABC):
    """Every question a broker has to answer, and nothing a part has to know.

    Subclasses are the only code permitted to know a broker's URLs, message
    shape or limits. An adapter registry (built alongside the first part that
    needs one, Task 3) resolves them by id from settings -- no part import
    ever names a broker.
    """

    @property
    @abc.abstractmethod
    def broker_id(self) -> str:
        """The id this broker is named by in settings and on the tape."""

    @abc.abstractmethod
    def declared_limits(self) -> Mapping[str, BrokerFact]:
        """Every capacity figure this adapter acts on, each with its source."""

    @abc.abstractmethod
    def token_policy(self) -> BrokerTokenPolicy:
        """How this broker's session token expires, and whether it can auto-refresh."""

    @abc.abstractmethod
    def instrument_listing_urls(self) -> tuple[str, ...]:
        """Where this broker's instrument master is fetched from, one URL per file."""

    @abc.abstractmethod
    def read_instrument_listings(self, response: object) -> tuple[InstrumentListing, ...]:
        """Every contract in one already-fetched, already-decoded listing file."""

    @abc.abstractmethod
    def stream_endpoint_url(self) -> str:
        """The websocket URL for this broker's market feed."""

    @abc.abstractmethod
    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        """Who pings whom, how often, and with what."""

    @abc.abstractmethod
    def connection_discipline(self) -> ConnectionDiscipline:
        """What this broker says about opening, holding and losing connections."""

    @abc.abstractmethod
    def encode_subscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        """The bytes that subscribe to these instrument/mode pairs."""

    @abc.abstractmethod
    def encode_unsubscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        """The bytes that unsubscribe from these instrument/mode pairs."""

    @abc.abstractmethod
    def does_subscription_fit_connection(
        self,
        existing: Sequence[SubscriptionRequest],
        candidate: SubscriptionRequest,
    ) -> bool:
        """Whether one more subscription fits alongside what a connection already carries.

        Upstox's limits are two-layered -- an individual cap per mode and a
        lower combined cap across modes on one connection -- so this cannot be
        a single counter the way a character-count or stream-count check
        would be (spec section 5)."""

    @abc.abstractmethod
    def decode_feed_message(self, payload: bytes) -> DecodedFeedMessage:
        """One WebSocket message, decomposed into this project's own record kinds."""

    @abc.abstractmethod
    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        """A ban this broker signalled over REST, or None if it did not."""


# Split the same way venue_adapter.py's are: these need no live broker data,
# so a conformance test can ask every adapter all of them with no network.
QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA = (
    "broker_id",
    "declared_limits",
    "token_policy",
    "instrument_listing_urls",
    "stream_endpoint_url",
    "heartbeat_discipline",
    "connection_discipline",
    "encode_subscribe_frame",
    "encode_unsubscribe_frame",
    "does_subscription_fit_connection",
)

# These need a real payload or a real listing response to ask meaningfully.
QUESTIONS_ANSWERED_FROM_BROKER_DATA = (
    "read_instrument_listings",
    "decode_feed_message",
    "read_http_ban_signal",
)


__all__ = [
    "BanSignal",
    "BrokerAdapter",
    "BrokerCandle",
    "BrokerFact",
    "BrokerFactWithoutSource",
    "BrokerOpenInterest",
    "BrokerOptionGreeks",
    "BrokerOrderBookLevel",
    "BrokerOrderBookUpdate",
    "BrokerTokenPolicy",
    "ConnectionDiscipline",
    "DecodedFeedMessage",
    "HeartbeatDiscipline",
    "InstrumentListing",
    "LtpUpdate",
    "QUESTIONS_ANSWERED_FROM_BROKER_DATA",
    "QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA",
    "SubscriptionMode",
    "SubscriptionRequest",
    "TokenExpiryPolicy",
]
```

- [ ] **Step 14: Run all of Task 1's tests, confirm everything passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_broker_adapter.py tests/runtime/test_tape.py -v
```
Expected: all PASS.

- [ ] **Step 15: Commit**

```bash
git add runtime/brokers/ tests/runtime/brokers/
git commit -m "feat: BrokerAdapter contract for Indian-market broker adapters

Fresh contract, not an extension of venue_adapter.py's crypto-perpetual
shape. Broker-agnostic (T-1, T-4) -- Upstox is the first of six planned
implementations, none of which change this file."
```

---

### Task 2: `UpstoxAdapter` — instrument listings, protobuf feed decode, subscription limits

**Files:**
- Create: `runtime/brokers/upstox_market_data_feed.proto`
- Create: `runtime/brokers/upstox_market_data_feed_pb2.py` (generated, committed)
- Create: `runtime/brokers/upstox.py`
- Test: `tests/runtime/brokers/test_upstox.py`
- Modify: `pyproject.toml` (add `protobuf==7.36.1`)

**Interfaces:**
- Consumes: everything from Task 1 (`BrokerAdapter` and all its data shapes)
- Produces: `runtime.brokers.upstox.UpstoxAdapter` (concrete `BrokerAdapter`), `runtime.brokers.upstox.UPSTOX_BROKER_ID = "upstox"`

- [ ] **Step 1: Commit the real `.proto` schema, verbatim**

```bash
mkdir -p runtime/brokers
curl -sL "https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto" -o /tmp/fetched.proto
diff /tmp/fetched.proto - <<'CHECK' || echo "SCHEMA CHANGED SINCE THIS PLAN WAS WRITTEN -- stop and re-read it before continuing"
syntax = "proto3";
package com.upstox.marketdatafeederv3udapi.rpc.proto;

message LTPC {
  double ltp = 1;
  int64 ltt = 2;
  int64 ltq = 3;
  double cp = 4;
}

message MarketLevel {
  repeated Quote bidAskQuote = 1;
}

message MarketOHLC {
  repeated OHLC ohlc = 1;
}

message Quote {
  int64 bidQ = 1;
  double bidP = 2;
  int64 askQ = 3;
  double askP = 4;
}

message OptionGreeks {
  double delta = 1;
  double theta = 2;
  double gamma = 3;
  double vega = 4;
  double rho = 5;
}

message OHLC {
  string interval = 1;
  double open = 2;
  double high = 3;
  double low = 4;
  double close = 5;
  int64 vol = 6;
  int64 ts = 7;
}

enum Type{
  initial_feed = 0;
  live_feed = 1;
  market_info = 2;
}

message MarketFullFeed{
  LTPC ltpc = 1;
  MarketLevel marketLevel = 2;
  OptionGreeks optionGreeks = 3;
  MarketOHLC marketOHLC = 4;
  double atp = 5; //avg traded price
  int64 vtt = 6; //volume traded today
  double oi = 7; //open interest
  double iv = 8; //implied volatility 
  double tbq =9; //total buy quantity
  double tsq = 10; //total sell quantity
}

message IndexFullFeed{
  LTPC ltpc = 1;
  MarketOHLC marketOHLC = 2;
}


message FullFeed {
  oneof FullFeedUnion {
    MarketFullFeed marketFF = 1;
    IndexFullFeed indexFF = 2;
  }
}

message FirstLevelWithGreeks{
  LTPC ltpc = 1;
  Quote firstDepth = 2;
  OptionGreeks optionGreeks = 3;
  int64 vtt = 4; //volume traded today
  double oi = 5; //open interest
  double iv = 6; //implied volatility 
}

message Feed {
  oneof FeedUnion {
    LTPC ltpc = 1;
    FullFeed fullFeed = 2;
    FirstLevelWithGreeks firstLevelWithGreeks = 3;
  }
  RequestMode requestMode = 4;
}

enum RequestMode {
  ltpc = 0;
  full_d5 = 1;
  option_greeks = 2;
  full_d30 = 3;
}

enum MarketStatus {
  PRE_OPEN_START = 0;
  PRE_OPEN_END = 1;
  NORMAL_OPEN = 2;
  NORMAL_CLOSE = 3;
  CLOSING_START = 4;
  CLOSING_END = 5;
}


message MarketInfo {
  map<string, MarketStatus> segmentStatus = 1;
}

message FeedResponse{
  Type type = 1;
  map<string, Feed> feeds = 2;
  int64 currentTs = 3;
  MarketInfo marketInfo = 4;
}
CHECK
```

If the diff matched (no "SCHEMA CHANGED" message), write the file with a header:

```bash
cat > runtime/brokers/upstox_market_data_feed.proto <<'EOF'
// Fetched verbatim from https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto
// on 2026-09-01. Upstox's own schema for its v3 WebSocket market feed
// (spec section 5). Do not hand-edit -- re-fetch and re-diff against this
// header comment if Upstox ever changes it, then regenerate the _pb2 file
// (Step 3 below) and re-run every test in this task.

EOF
cat /tmp/fetched.proto >> runtime/brokers/upstox_market_data_feed.proto
```

- [ ] **Step 2: Add `protobuf` and `grpcio-tools` (codegen only)**

```bash
uv add protobuf==7.36.1
```

Then edit `pyproject.toml`'s `dependencies` list to add the comment (matching the existing style — every entry has one):

```toml
    # Decodes Upstox's protobuf-encoded v3 market feed (spec section 5) --
    # the only broker adapter needing this so far, but any future adapter
    # streaming protobuf reuses it rather than adding a second decoder.
    # cp310-abi3 wheel, forward-compatible with 3.14, no compiler needed.
    "protobuf==7.36.1",
```

`grpcio-tools` is codegen-only (used once in Step 3, never imported at runtime) — install it into the venv without adding it to `pyproject.toml`'s tracked dependencies:

```bash
uv pip install grpcio-tools==1.83.1
```

- [ ] **Step 3: Generate the `_pb2.py` file and commit it**

```bash
cd runtime/brokers
.venv/../.venv/bin/python3 -m grpc_tools.protoc \
  --proto_path=. \
  --python_out=. \
  upstox_market_data_feed.proto
cd -
```

(Adjust the venv python path to wherever `uv pip install` put `grpc_tools` — verify with `.venv/bin/python3 -m grpc_tools.protoc --version` first if the above path is wrong for this checkout.)

Verify it imports:

```bash
.venv/bin/python3 -c "from runtime.brokers import upstox_market_data_feed_pb2 as pb2; print(pb2.FeedResponse.DESCRIPTOR.full_name)"
```
Expected output: `com.upstox.marketdatafeederv3udapi.rpc.proto.FeedResponse`

- [ ] **Step 4: Write the failing test for instrument listing parsing, using Upstox's own real documented sample**

```python
# tests/runtime/brokers/test_upstox.py
"""Tests against Upstox's own documented sample payloads, fetched 2026-09-01
from upstox.com/developer/api-documentation/instruments and .../v3/get-market-data-feed
(cited per-test below), and against real protobuf wire encoding built from
the actual committed schema. Never hand-invented field values for a shape
Upstox hasn't published somewhere."""

from runtime.brokers.upstox import UpstoxAdapter


def test_reads_equity_instrument_listing_from_upstox_own_sample():
    # Source: upstox.com/developer/api-documentation/instruments, "EQ" sample,
    # fetched 2026-09-01.
    response = [
        {
            "segment": "NSE_EQ",
            "name": "JOCIL LIMITED",
            "exchange": "NSE",
            "isin": "INE839G01010",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE839G01010",
            "lot_size": 1,
            "freeze_quantity": 100000.0,
            "exchange_token": "16927",
            "tick_size": 5.0,
            "trading_symbol": "JOCIL",
            "short_name": "JOCIL",
            "security_type": "NORMAL",
            "cas_eligible": True,
        }
    ]
    adapter = UpstoxAdapter()
    listings = adapter.read_instrument_listings(response)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.instrument_key == "NSE_EQ|INE839G01010"
    assert listing.exchange == "NSE"
    assert listing.segment == "NSE_EQ"
    assert listing.instrument_type == "EQ"
    assert listing.lot_size == 1
    assert listing.tick_size == 5.0
    assert listing.expiry_ms is None
    assert listing.strike_price is None


def test_reads_option_instrument_listing_with_strike_and_expiry():
    # Source: same page, "Options" sample, fetched 2026-09-01.
    response = [
        {
            "weekly": False,
            "segment": "NSE_FO",
            "name": "VODAFONE IDEA LIMITED",
            "exchange": "NSE",
            "expiry": 1706207399000,
            "instrument_type": "CE",
            "underlying_symbol": "IDEA",
            "instrument_key": "NSE_FO|36708",
            "lot_size": 80000,
            "freeze_quantity": 1600000.0,
            "exchange_token": "36708",
            "minimum_lot": 80000,
            "underlying_key": "NSE_EQ|INE669E01016",
            "tick_size": 5.0,
            "underlying_type": "EQUITY",
            "trading_symbol": "IDEA 22 CE 25 JAN 24",
            "strike_price": 22.0,
        }
    ]
    adapter = UpstoxAdapter()
    listing = adapter.read_instrument_listings(response)[0]
    assert listing.instrument_type == "CE"
    assert listing.expiry_ms == 1706207399000
    assert listing.strike_price == 22.0
    assert listing.underlying_key == "NSE_EQ|INE669E01016"
```

- [ ] **Step 5: Run it, confirm it fails** (`ModuleNotFoundError: runtime.brokers.upstox`)

- [ ] **Step 6: Implement `UpstoxAdapter` — identity, limits, listings**

```python
# runtime/brokers/upstox.py
"""UpstoxAdapter: everything Upstox-specific, and the only place it is allowed
to live (docs/superpowers/specs/2026-09-01-upstox-adapter-design.md).
"""

from __future__ import annotations

import datetime
from typing import Mapping, Sequence

from runtime.brokers.broker_adapter import (
    BanSignal,
    BrokerAdapter,
    BrokerCandle,
    BrokerFact,
    BrokerOpenInterest,
    BrokerOptionGreeks,
    BrokerOrderBookLevel,
    BrokerOrderBookUpdate,
    BrokerTokenPolicy,
    ConnectionDiscipline,
    DecodedFeedMessage,
    HeartbeatDiscipline,
    InstrumentListing,
    LtpUpdate,
    SubscriptionMode,
    SubscriptionRequest,
    TokenExpiryPolicy,
)
from runtime.brokers import upstox_market_data_feed_pb2 as feed_pb2

UPSTOX_BROKER_ID = "upstox"

# Source for every figure below: upstox.com/developer/api-documentation/v3/get-market-data-feed,
# fetched 2026-09-01. Free-tier limits -- Upstox Plus limits are a settings
# question for whenever that tier is actually bought, not baked in here.
_LIMITS_SOURCE = "upstox v3 market-data-feed docs, fetched 2026-09-01"


class UpstoxAdapter(BrokerAdapter):
    @property
    def broker_id(self) -> str:
        return UPSTOX_BROKER_ID

    def declared_limits(self) -> Mapping[str, BrokerFact]:
        return {
            "connections_per_user": BrokerFact(
                name="connections_per_user", value=2, unit="count", source=_LIMITS_SOURCE
            ),
            "ltpc_individual_limit": BrokerFact(
                name="ltpc_individual_limit", value=5000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "ltpc_combined_limit": BrokerFact(
                name="ltpc_combined_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "option_greeks_individual_limit": BrokerFact(
                name="option_greeks_individual_limit", value=3000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "option_greeks_combined_limit": BrokerFact(
                name="option_greeks_combined_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "full_individual_limit": BrokerFact(
                name="full_individual_limit", value=2000, unit="instrument keys", source=_LIMITS_SOURCE
            ),
            "full_combined_limit": BrokerFact(
                name="full_combined_limit", value=1500, unit="instrument keys", source=_LIMITS_SOURCE
            ),
        }

    def token_policy(self) -> BrokerTokenPolicy:
        return BrokerTokenPolicy(
            expiry=TokenExpiryPolicy.DAILY_AT_FIXED_TIME,
            daily_expiry_time_ist=datetime.time(3, 30),
            # True because upstox-totp (spec section 3) automates the whole
            # login -> code -> token exchange without a human. Not true of
            # Upstox's own documented flows on their own.
            auto_refreshable=True,
            source="upstox.com/developer/api-documentation/get-token, fetched 2026-09-01; "
                   "auto_refreshable via upstox-totp 1.0.8 (PyPI, MIT)",
        )

    def instrument_listing_urls(self) -> tuple[str, ...]:
        # NSE and BSE only -- MCX (commodities) is out of scope until that
        # segment is built (docs/goal.md #6, deferred).
        return (
            "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
            "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
        )

    def read_instrument_listings(self, response: object) -> tuple[InstrumentListing, ...]:
        listings = []
        for row in response:
            listings.append(
                InstrumentListing(
                    instrument_key=row["instrument_key"],
                    exchange=row["exchange"],
                    segment=row["segment"],
                    instrument_type=row["instrument_type"],
                    trading_symbol=row.get("trading_symbol", ""),
                    lot_size=row.get("lot_size"),
                    tick_size=row.get("tick_size"),
                    freeze_quantity=row.get("freeze_quantity"),
                    expiry_ms=row.get("expiry"),
                    strike_price=row.get("strike_price"),
                    underlying_key=row.get("underlying_key"),
                    intraday_margin_percent=row.get("intraday_margin"),
                    intraday_leverage=row.get("intraday_leverage"),
                )
            )
        return tuple(listings)
```

- [ ] **Step 7: Run Step 4's test, confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_upstox.py -v
```

- [ ] **Step 8: Write the failing test for protobuf feed decoding, built with the real generated classes**

```python
def test_decodes_a_market_full_feed_ltpc_and_book_and_open_interest():
    # Built with the ACTUAL generated classes from the committed .proto --
    # real wire format, test-chosen field values (Global Constraints: this
    # is the honest boundary of RL-063 before a live account exists).
    feed = feed_pb2.Feed()
    feed.fullFeed.marketFF.ltpc.ltp = 219.3
    feed.fullFeed.marketFF.ltpc.ltt = 1740729552723
    feed.fullFeed.marketFF.ltpc.ltq = 75
    feed.fullFeed.marketFF.ltpc.cp = 494.05
    feed.fullFeed.marketFF.marketLevel.bidAskQuote.add(bidQ=75, bidP=225.4, askQ=150, askP=225.7)
    feed.fullFeed.marketFF.oi = 256800
    feed.fullFeed.marketFF.vtt = 919725
    feed.fullFeed.marketFF.tbq = 100.0
    feed.fullFeed.marketFF.tsq = 50.0
    feed.requestMode = feed_pb2.full_d5

    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.live_feed
    response.currentTs = 1740729566039
    response.feeds["NSE_FO|45450"].CopyFrom(feed)
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert decoded.kind == "live_feed"
    assert decoded.broker_time_ns == 1740729566039 * 1_000_000
    assert len(decoded.ltp_updates) == 1
    assert decoded.ltp_updates[0].instrument_key == "NSE_FO|45450"
    assert decoded.ltp_updates[0].last_traded_price == 219.3
    assert len(decoded.book_updates) == 1
    assert decoded.book_updates[0].levels[0].bid_price == 225.4
    assert len(decoded.open_interest) == 1
    assert decoded.open_interest[0].open_interest == 256800


def test_decodes_a_market_info_message():
    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.market_info
    response.currentTs = 1732775008661
    response.marketInfo.segmentStatus["NSE_EQ"] = feed_pb2.NORMAL_OPEN
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert decoded.kind == "market_info"
    assert decoded.market_segment_status == {"NSE_EQ": "NORMAL_OPEN"}
    assert decoded.ltp_updates == ()


def test_decodes_index_full_feed_with_no_book_or_greeks():
    # IndexFullFeed carries only ltpc + OHLC -- indices aren't traded
    # directly, so there's no book or open interest to decode (spec: the
    # oneof's second branch).
    feed = feed_pb2.Feed()
    feed.fullFeed.indexFF.ltpc.ltp = 24500.0
    feed.fullFeed.indexFF.ltpc.ltt = 1740729552723
    feed.fullFeed.indexFF.marketOHLC.ohlc.add(interval="1d", open=24400.0, high=24600.0, low=24350.0, close=24500.0, vol=0, ts=1740681000000)
    response = feed_pb2.FeedResponse()
    response.type = feed_pb2.live_feed
    response.feeds["NSE_INDEX|Nifty 50"].CopyFrom(feed)
    payload = response.SerializeToString()

    adapter = UpstoxAdapter()
    decoded = adapter.decode_feed_message(payload)

    assert len(decoded.ltp_updates) == 1
    assert decoded.ltp_updates[0].last_traded_price == 24500.0
    assert len(decoded.candles) == 1
    assert decoded.candles[0].is_closed is None  # never stated by this feed
    assert decoded.open_interest == ()
    assert decoded.book_updates == ()
```

- [ ] **Step 9: Run it, confirm it fails** (`decode_feed_message` not implemented)

- [ ] **Step 10: Implement `decode_feed_message`**

Append to `runtime/brokers/upstox.py`:

```python
    def stream_endpoint_url(self) -> str:
        return "wss://api.upstox.com/v3/feed/market-data-feed"

    def heartbeat_discipline(self) -> HeartbeatDiscipline:
        # Standard WS ping/pong, handled by most client libraries -- Upstox
        # sends the ping frame itself (spec section 5), simpler than a venue
        # requiring an application-level ping.
        return HeartbeatDiscipline(expects_client_ping=False)

    def connection_discipline(self) -> ConnectionDiscipline:
        return ConnectionDiscipline(concurrent_connections=2)

    def encode_subscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        raise NotImplementedError(
            "subscribe-frame encoding needs a live sandbox check of whether "
            "the request itself is protobuf or JSON-then-binary (spec "
            "section 5 flags this as unresolved) -- deferred to Task 5, "
            "which is the first and only caller."
        )

    def encode_unsubscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        raise NotImplementedError("see encode_subscribe_frame")

    def does_subscription_fit_connection(
        self,
        existing: Sequence[SubscriptionRequest],
        candidate: SubscriptionRequest,
    ) -> bool:
        limits = self.declared_limits()
        by_mode: dict[SubscriptionMode, int] = {}
        for request in existing:
            by_mode[request.mode] = by_mode.get(request.mode, 0) + 1
        individual_key = {
            SubscriptionMode.LTPC: "ltpc_individual_limit",
            SubscriptionMode.OPTION_GREEKS: "option_greeks_individual_limit",
            SubscriptionMode.FULL: "full_individual_limit",
        }.get(candidate.mode)
        combined_key = {
            SubscriptionMode.LTPC: "ltpc_combined_limit",
            SubscriptionMode.OPTION_GREEKS: "option_greeks_combined_limit",
            SubscriptionMode.FULL: "full_combined_limit",
        }.get(candidate.mode)
        if individual_key is None:
            # FULL_D30 is an Upstox Plus mode -- no free-tier limit is
            # declared for it, so a candidate asking for it is refused rather
            # than silently allowed past a check that has nothing to check.
            return False
        candidate_count_in_mode = by_mode.get(candidate.mode, 0) + 1
        if candidate_count_in_mode > int(limits[individual_key].value):
            return False
        modes_in_use = set(by_mode) | {candidate.mode}
        if len(modes_in_use) > 1:
            total_after = len(existing) + 1
            if total_after > int(limits[combined_key].value):
                return False
        return True

    def decode_feed_message(self, payload: bytes) -> DecodedFeedMessage:
        response = feed_pb2.FeedResponse()
        response.ParseFromString(payload)

        kind = feed_pb2.Type.Name(response.type)
        segment_status = None
        if response.HasField("marketInfo"):
            segment_status = {
                segment: feed_pb2.MarketStatus.Name(status)
                for segment, status in response.marketInfo.segmentStatus.items()
            }

        broker_time_ns = response.currentTs * 1_000_000 if response.currentTs else 0

        ltp_updates: list[LtpUpdate] = []
        candles: list[BrokerCandle] = []
        book_updates: list[BrokerOrderBookUpdate] = []
        open_interest: list[BrokerOpenInterest] = []
        option_greeks: list[BrokerOptionGreeks] = []

        for instrument_key, feed in response.feeds.items():
            which = feed.WhichOneof("FeedUnion")
            if which == "ltpc":
                ltp_updates.append(self._read_ltpc(instrument_key, feed.ltpc, broker_time_ns))
            elif which == "firstLevelWithGreeks":
                first = feed.firstLevelWithGreeks
                ltp_updates.append(self._read_ltpc(instrument_key, first.ltpc, broker_time_ns))
                book_updates.append(
                    BrokerOrderBookUpdate(
                        instrument_key=instrument_key,
                        levels=(self._read_quote_level(first.firstDepth),),
                        broker_time_ns=broker_time_ns,
                    )
                )
                option_greeks.append(
                    self._read_greeks(instrument_key, first.optionGreeks, first.iv, broker_time_ns)
                )
                open_interest.append(
                    BrokerOpenInterest(
                        instrument_key=instrument_key,
                        open_interest=first.oi,
                        volume_traded_today=first.vtt,
                        total_buy_quantity=0.0,
                        total_sell_quantity=0.0,
                        average_traded_price=0.0,
                        broker_time_ns=broker_time_ns,
                    )
                )
            elif which == "fullFeed":
                full = feed.fullFeed
                full_which = full.WhichOneof("FullFeedUnion")
                if full_which == "marketFF":
                    market = full.marketFF
                    ltp_updates.append(self._read_ltpc(instrument_key, market.ltpc, broker_time_ns))
                    if market.HasField("marketLevel"):
                        book_updates.append(
                            BrokerOrderBookUpdate(
                                instrument_key=instrument_key,
                                levels=tuple(
                                    self._read_quote_level(level)
                                    for level in market.marketLevel.bidAskQuote
                                ),
                                broker_time_ns=broker_time_ns,
                            )
                        )
                    for bar in market.marketOHLC.ohlc:
                        candles.append(self._read_ohlc(instrument_key, bar))
                    if market.HasField("optionGreeks"):
                        option_greeks.append(
                            self._read_greeks(
                                instrument_key, market.optionGreeks, market.iv, broker_time_ns
                            )
                        )
                    open_interest.append(
                        BrokerOpenInterest(
                            instrument_key=instrument_key,
                            open_interest=market.oi,
                            volume_traded_today=market.vtt,
                            total_buy_quantity=market.tbq,
                            total_sell_quantity=market.tsq,
                            average_traded_price=market.atp,
                            broker_time_ns=broker_time_ns,
                        )
                    )
                elif full_which == "indexFF":
                    index = full.indexFF
                    ltp_updates.append(self._read_ltpc(instrument_key, index.ltpc, broker_time_ns))
                    for bar in index.marketOHLC.ohlc:
                        candles.append(self._read_ohlc(instrument_key, bar))

        return DecodedFeedMessage(
            kind=kind,
            market_segment_status=segment_status,
            ltp_updates=tuple(ltp_updates),
            candles=tuple(candles),
            book_updates=tuple(book_updates),
            open_interest=tuple(open_interest),
            option_greeks=tuple(option_greeks),
            broker_time_ns=broker_time_ns,
        )

    @staticmethod
    def _read_ltpc(instrument_key: str, ltpc, broker_time_ns: int) -> LtpUpdate:
        return LtpUpdate(
            instrument_key=instrument_key,
            last_traded_price=ltpc.ltp,
            last_traded_quantity=ltpc.ltq or None,
            last_traded_time_ms=ltpc.ltt,
            close_price=ltpc.cp or None,
            broker_time_ns=broker_time_ns,
        )

    @staticmethod
    def _read_quote_level(quote) -> BrokerOrderBookLevel:
        return BrokerOrderBookLevel(
            bid_price=quote.bidP, bid_quantity=quote.bidQ,
            ask_price=quote.askP, ask_quantity=quote.askQ,
        )

    @staticmethod
    def _read_ohlc(instrument_key: str, bar) -> BrokerCandle:
        return BrokerCandle(
            instrument_key=instrument_key,
            interval=bar.interval,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar.vol,
            bar_time_ms=bar.ts,
            # Upstox states no closed/live flag on any OHLC entry (verified
            # against the committed .proto -- OHLC carries no such field).
            # None here is the honest reading, not a guess.
            is_closed=None,
        )

    @staticmethod
    def _read_greeks(
        instrument_key: str, greeks, implied_volatility: float, broker_time_ns: int
    ) -> BrokerOptionGreeks:
        # `iv` lives on the parent message (MarketFullFeed/FirstLevelWithGreeks),
        # not inside the OptionGreeks submessage itself -- verified against the
        # committed .proto -- so every caller passes its own sibling `iv` field
        # in rather than this method reading a field that doesn't exist on `greeks`.
        return BrokerOptionGreeks(
            instrument_key=instrument_key,
            delta=greeks.delta, theta=greeks.theta, gamma=greeks.gamma,
            vega=greeks.vega, rho=greeks.rho,
            implied_volatility=implied_volatility,
            broker_time_ns=broker_time_ns,
        )

    def read_http_ban_signal(
        self, status_code: int, headers: Mapping[str, str]
    ) -> BanSignal | None:
        if status_code == 429:
            retry_after = headers.get("Retry-After")
            return BanSignal(
                broker_id=UPSTOX_BROKER_ID,
                observed_code=str(status_code),
                reason="rate limited",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )
        return None
```

- [ ] **Step 11: Run all of Task 2's tests, confirm everything passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_upstox.py -v
```

- [ ] **Step 12: Commit**

```bash
git add runtime/brokers/ tests/runtime/brokers/test_upstox.py pyproject.toml
git commit -m "feat: UpstoxAdapter -- instrument listings and protobuf feed decode

Instrument parsing tested against Upstox's own documented sample rows.
Feed decode tested against real protobuf wire encoding built from the
committed .proto (fetched and verified 2026-09-01), not hand-rolled
bytes. subscribe/unsubscribe frame encoding deferred to Task 5, the
first real caller -- whether the request itself is protobuf or
JSON-then-binary needs a live sandbox check the spec already flagged
as unresolved."
```

---

### Task 3: `broker-token-refresh-scheduler`

**Files:**
- Create: `parts/broker_adapter/__init__.py`
- Create: `parts/broker_adapter/broker_token_refresh_scheduler.py`
- Test: `tests/parts/broker_adapter/test_broker_token_refresh_scheduler.py`
- Modify: `pyproject.toml` (add `upstox-totp==1.0.8`)

**Interfaces:**
- Consumes: nothing (reads settings + secrets directly, per `docs/features.json`'s declaration)
- Produces: `broker-token-standing` (a `TokenStanding` dataclass this task defines: `broker_id`, `is_valid`, `expires_at_ist`, `refreshed_at_ns`, `last_failure`)

- [ ] **Step 1: Add `upstox-totp` dependency**

```bash
uv add upstox-totp==1.0.8
```

Add the comment to `pyproject.toml`, matching house style:

```toml
    # Fully-automated daily Upstox login (mobile+password+PIN+TOTP -> access
    # token), so the token-refresh-scheduler part needs no human click.
    # MIT, actively maintained (PyPI, checked 2026-09-01). Unofficial --
    # replays Upstox's internal login endpoints, same as every retail algo
    # trader's approach to this; see docs/secrets.md for where the
    # credentials it needs are stored.
    "upstox-totp==1.0.8",
```

- [ ] **Step 2: Write the failing test for `TokenStanding.is_still_valid`**

```python
# tests/parts/broker_adapter/test_broker_token_refresh_scheduler.py
import datetime

from zoneinfo import ZoneInfo

from parts.broker_adapter.broker_token_refresh_scheduler import TokenStanding

IST = ZoneInfo("Asia/Kolkata")


def test_token_generated_before_expiry_time_is_valid_same_day():
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)  # 9 AM
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 1, 15, 0, tzinfo=IST)  # 3 PM same day
    assert standing.is_still_valid(check_at)


def test_token_is_invalid_after_next_days_expiry_time():
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 2, 4, 0, tzinfo=IST)  # 4 AM next day
    assert not standing.is_still_valid(check_at)


def test_token_generated_after_expiry_time_expires_the_following_day():
    # Generated at 5 AM -- after that day's 3:30 AM boundary -- so the next
    # boundary is tomorrow's 3:30, not today's already-passed one.
    generated = datetime.datetime(2026, 9, 1, 5, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 2, 3, 0, tzinfo=IST)  # still before tomorrow's 3:30
    assert standing.is_still_valid(check_at)
```

- [ ] **Step 3: Run it, confirm it fails**

- [ ] **Step 4: Implement `TokenStanding`**

```python
# parts/broker_adapter/__init__.py
```
(empty)

```python
# parts/broker_adapter/broker_token_refresh_scheduler.py
"""broker-token-refresh-scheduler: keep today's broker session token valid.

Runs once daily, before market open, via TOTP auto-login (spec section 3) --
no human click. Same shape as the crypto build's KiteAccessTokenFileStore
precedent (ajith4134/nse-botonly), rebuilt against Upstox's daily 3:30 AM
IST expiry rather than Zerodha's ~6 AM one.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-token-refresh-scheduler"
IST = ZoneInfo("Asia/Kolkata")

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("broker-token-standing", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

DEFAULT_TOKEN_FILE_PATH = Path("~/.local/share/ajit-segment-bots/broker_tokens/upstox.json").expanduser()


@dataclasses.dataclass(frozen=True)
class TokenStanding:
    """Whether today's token is still usable right now, and until when.

    `daily_expiry_time_ist` comes from BrokerTokenPolicy.daily_expiry_time_ist
    -- this part does not know Upstox's 3:30 AM figure itself, the adapter
    does (T-4).
    """

    broker_id: str
    access_token: str
    generated_at: datetime.datetime  # timezone-aware
    daily_expiry_time_ist: datetime.time
    last_failure: str | None = None

    def __repr__(self) -> str:  # the token is a secret; never leak it
        return (
            f"TokenStanding(broker_id={self.broker_id!r}, access_token='***', "
            f"generated_at={self.generated_at!r})"
        )

    def expires_at(self) -> datetime.datetime:
        generated_ist = self.generated_at.astimezone(IST)
        same_day_expiry = datetime.datetime.combine(
            generated_ist.date(), self.daily_expiry_time_ist, IST
        )
        if generated_ist < same_day_expiry:
            return same_day_expiry
        return same_day_expiry + datetime.timedelta(days=1)

    def is_still_valid(self, now: datetime.datetime | None = None) -> bool:
        now_ist = (now or datetime.datetime.now(IST)).astimezone(IST)
        return now_ist < self.expires_at()
```

- [ ] **Step 5: Run Step 2's tests, confirm they pass**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_token_refresh_scheduler.py -v
```

- [ ] **Step 6: Write the failing test for the file store (save/load round trip)**

```python
def test_token_file_store_round_trips(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import TokenFileStore

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="secret-token", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    store.save(standing)

    loaded = store.load()
    assert loaded.access_token == "secret-token"
    assert loaded.broker_id == "upstox"
    assert loaded.generated_at == generated


def test_token_file_store_is_owner_only(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import TokenFileStore
    import stat

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="secret", generated_at=datetime.datetime.now(IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    mode = stat.S_IMODE((tmp_path / "upstox.json").stat().st_mode)
    assert mode == 0o600
```

- [ ] **Step 7: Run it, confirm it fails**

- [ ] **Step 8: Implement `TokenFileStore`**

```python
class TokenFileStore:
    """Persists TokenStanding to a chmod-600 file, outside sops -- it rotates
    daily and doesn't need sops's protection the way a standing password
    does (spec section 8)."""

    def __init__(self, token_file_path: Path = DEFAULT_TOKEN_FILE_PATH) -> None:
        self._token_file_path = token_file_path

    def save(self, standing: TokenStanding) -> None:
        self._token_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_file_path.write_text(json.dumps({
            "broker_id": standing.broker_id,
            "access_token": standing.access_token,
            "generated_at": standing.generated_at.isoformat(),
            "daily_expiry_time_ist": standing.daily_expiry_time_ist.isoformat(),
        }))
        os.chmod(self._token_file_path, 0o600)

    def load(self) -> TokenStanding | None:
        if not self._token_file_path.exists():
            return None
        fields = json.loads(self._token_file_path.read_text())
        return TokenStanding(
            broker_id=fields["broker_id"],
            access_token=fields["access_token"],
            generated_at=datetime.datetime.fromisoformat(fields["generated_at"]),
            daily_expiry_time_ist=datetime.time.fromisoformat(fields["daily_expiry_time_ist"]),
        )
```

- [ ] **Step 9: Run Step 6's tests, confirm they pass**

- [ ] **Step 10: Write the failing test for `refresh_if_needed` — the scheduling logic itself, with the TOTP client injected**

```python
def test_refresh_if_needed_skips_when_token_still_valid(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, TokenStanding, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    now = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    store.save(TokenStanding(
        broker_id="upstox", access_token="still-good",
        generated_at=datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))

    calls = []
    def fake_generate_token():
        calls.append(1)
        return "should-not-be-called"

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=fake_generate_token, now=now,
    )
    assert standing.access_token == "still-good"
    assert calls == []


def test_refresh_if_needed_refreshes_when_token_expired(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, TokenStanding, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="stale",
        generated_at=datetime.datetime(2026, 8, 31, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    now = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=IST)  # past yesterday's expiry

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=lambda: "fresh-token", now=now,
    )
    assert standing.access_token == "fresh-token"
    assert store.load().access_token == "fresh-token"


def test_refresh_if_needed_records_failure_and_keeps_the_stale_token(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, TokenStanding, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="stale",
        generated_at=datetime.datetime(2026, 8, 31, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    now = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=IST)

    def failing_generate_token():
        raise RuntimeError("login failed: bad TOTP")

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=failing_generate_token, now=now,
    )
    # The stale token is kept and returned rather than dropped -- a failed
    # refresh must not leave every consumer with nothing at all.
    assert standing.access_token == "stale"
    assert "bad TOTP" in standing.last_failure
```

- [ ] **Step 11: Run it, confirm it fails**

- [ ] **Step 12: Implement `refresh_if_needed`, then wire `start_part`**

```python
def refresh_if_needed(
    store: TokenFileStore,
    daily_expiry_time_ist: datetime.time,
    generate_token,
    now: datetime.datetime | None = None,
) -> TokenStanding:
    """The scheduling core: refresh only when the stored token is no longer
    valid, keep the stale token on a failed refresh rather than discarding it.

    `generate_token` is a zero-argument callable returning a fresh access
    token string, or raising. Injected so this function never imports
    upstox_totp itself -- start_part below is the only place that does,
    which is what makes this testable without real credentials.
    """
    now = now or datetime.datetime.now(IST)
    existing = store.load()
    if existing is not None and existing.is_still_valid(now):
        return existing
    try:
        token = generate_token()
    except Exception as failure:
        if existing is not None:
            failed = dataclasses.replace(existing, last_failure=f"{type(failure).__name__}: {failure}")
            store.save(failed)
            return failed
        raise
    standing = TokenStanding(
        broker_id="upstox", access_token=token, generated_at=now,
        daily_expiry_time_ist=daily_expiry_time_ist,
    )
    store.save(standing)
    return standing


def describe_standing(standing: TokenStanding | None) -> dict:
    if standing is None:
        return {"part_id": PART_ID, "has_token": False}
    return {
        "part_id": PART_ID,
        "has_token": True,
        "broker_id": standing.broker_id,
        "is_valid": standing.is_still_valid(),
        "generated_at": standing.generated_at.isoformat(),
        "last_failure": standing.last_failure,
    }


def start_part(context) -> int:
    """T-1's one entry point. Reads login credentials from the sops+age store
    (docs/secrets.md), never from settings -- settings are the operator's
    tunable numbers, secrets are secrets, and RUNTIME_SCOPE_NAME's settings
    document only carries the refresh-check interval.
    """
    from upstox_totp import UpstoxTOTP

    settings = context.settings[RUNTIME_SCOPE_NAME]
    store = TokenFileStore()

    def generate_token() -> str:
        upx = UpstoxTOTP()  # auto-loads UPSTOX_* env vars, sourced from the
                             # sops store at process start -- see Task 3's
                             # deployment note below, this is not wired here.
        response = upx.app_token.get_access_token()
        if not response.success or not response.data:
            raise RuntimeError(f"upstox-totp login did not succeed: {response}")
        return response.data.access_token

    publish_standing = context.bus.publisher_for("broker-token-standing")
    daily_expiry_time_ist = datetime.time(3, 30)  # BrokerTokenPolicy's own
                                                    # figure (Task 2); read
                                                    # from an UpstoxAdapter
                                                    # instance once Task 5's
                                                    # adapter_registry exists,
                                                    # not duplicated here.
    current = [None]

    def refresh_if_due() -> None:
        current[0] = refresh_if_needed(
            store=store, daily_expiry_time_ist=daily_expiry_time_ist,
            generate_token=generate_token,
        )
        publish_standing(current[0])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=refresh_if_due,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(current[0]),
    )
```

- [ ] **Step 13: Run all of Task 3's tests, confirm everything passes**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_token_refresh_scheduler.py -v
```

- [ ] **Step 14: Add the settings entry this part reads**

Append to `~/.config/ajit-segment-bots/settings/runtime.toml` (machine-local, not git-tracked — matches every existing entry in that file):

```toml
[broker_token_refresh_check_interval]
value = 300
unit  = "seconds"
note  = "Claude, 2026-09-01: how often broker-token-refresh-scheduler checks whether today's Upstox token has expired. Five minutes -- far shorter than the 24-hour token lifetime, so a token that expires at 3:30 AM IST is refreshed within minutes rather than waiting for the next tick's health interval to happen to fall after the boundary."
```

(This value isn't consumed by the code above yet — Task 3 as written checks on every tick via `run_part`'s own health-interval cadence, same as `symbol_catalogue_reader.py`'s `read_if_due` pattern. If a coarser check interval is wanted, thread this setting into `refresh_if_due` the same way `symbol_catalogue_reader.py` threads `refresh_interval_seconds` — noted here rather than silently applied, since `TokenStanding.is_still_valid` already makes over-checking cheap and harmless.)

- [ ] **Step 15: Commit**

```bash
git add parts/broker_adapter/ tests/parts/broker_adapter/ pyproject.toml
git commit -m "feat: broker-token-refresh-scheduler part

TOTP auto-login via upstox-totp, injected as generate_token so the
scheduling core (refresh_if_needed) is fully tested without real
credentials. Token persisted to a chmod-600 file outside sops -- it
rotates daily and doesn't need sops's protection the way the standing
password/TOTP-secret credentials do."
```

---

### Task 4: `broker-instrument-catalogue-reader`

**Files:**
- Create: `parts/broker_adapter/broker_instrument_catalogue_reader.py`
- Test: `tests/parts/broker_adapter/test_broker_instrument_catalogue_reader.py`

**Interfaces:**
- Consumes: nothing (Upstox's instrument files are public, no token needed — Task 2's `UpstoxAdapter.instrument_listing_urls()`)
- Produces: `broker-instrument-listing` (the `tuple[InstrumentListing, ...]` from Task 1/2, published whole)

- [ ] **Step 1: Write the failing test for gzip-fetch-and-parse, against a real gzipped payload built from Upstox's own sample row**

```python
# tests/parts/broker_adapter/test_broker_instrument_catalogue_reader.py
import gzip
import json

from parts.broker_adapter.broker_instrument_catalogue_reader import fetch_and_parse_listings
from runtime.brokers.upstox import UpstoxAdapter


def test_fetch_and_parse_listings_reads_a_gzipped_json_array():
    # Same real sample row as Task 2's test, gzipped the way Upstox actually
    # serves these files -- the shape under test here is the gzip+JSON
    # handling, not the row parsing (already covered in Task 2).
    rows = [{
        "segment": "NSE_EQ", "name": "JOCIL LIMITED", "exchange": "NSE",
        "isin": "INE839G01010", "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE839G01010", "lot_size": 1,
        "freeze_quantity": 100000.0, "exchange_token": "16927",
        "tick_size": 5.0, "trading_symbol": "JOCIL",
    }]
    gzipped = gzip.compress(json.dumps(rows).encode("utf-8"))

    fetched_urls = []
    def fake_fetch(url: str) -> bytes:
        fetched_urls.append(url)
        return gzipped

    adapter = UpstoxAdapter()
    listings = fetch_and_parse_listings(adapter, fetch=fake_fetch)

    assert len(listings) == 2  # one per URL -- NSE.json.gz and BSE.json.gz, same fixture for both
    assert listings[0].instrument_key == "NSE_EQ|INE839G01010"
    assert fetched_urls == list(adapter.instrument_listing_urls())
```

- [ ] **Step 2: Run it, confirm it fails**

- [ ] **Step 3: Implement `fetch_and_parse_listings` and the part**

```python
# parts/broker_adapter/broker_instrument_catalogue_reader.py
"""broker-instrument-catalogue-reader: every tradable contract, as the
broker's own daily instrument master lists it.

Static gzipped files refreshed once a day around 6 AM IST (spec section 4)
-- not a paginated REST catalogue the way the crypto build's
symbol-catalogue-reader followed. No cursor loop, no per-page request.
"""

from __future__ import annotations

import gzip
import json
import urllib.request

from runtime.brokers.broker_adapter import BrokerAdapter, InstrumentListing
from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-instrument-catalogue-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("broker-instrument-listing", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


def fetch_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/gzip"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def fetch_and_parse_listings(
    adapter: BrokerAdapter, fetch=fetch_bytes
) -> tuple[InstrumentListing, ...]:
    """Every listing from every URL the adapter names, gunzipped and parsed.

    One call per URL, never a cursor loop -- these are whole files, not
    paginated responses (spec section 4)."""
    listings: list[InstrumentListing] = []
    for url in adapter.instrument_listing_urls():
        raw = fetch(url)
        rows = json.loads(gzip.decompress(raw).decode("utf-8"))
        listings.extend(adapter.read_instrument_listings(rows))
    return tuple(listings)


def describe_standing(listings: tuple, last_failure: str | None) -> dict:
    return {
        "part_id": PART_ID,
        "listings_seen": len(listings),
        "last_failure": last_failure,
    }


def start_part(context) -> int:
    """One reader, one broker for now (Upstox) -- a settings-driven adapter
    registry follows the same pattern as venue_adapter's once a second
    broker is actually built (Task 2's docstring), not invented ahead of
    that need.
    """
    from runtime.brokers.upstox import UpstoxAdapter

    adapter = UpstoxAdapter()
    publish_listings = context.bus.publisher_for("broker-instrument-listing")
    refresh_interval_seconds = context.number("broker_catalogue_refresh_interval")

    state = {"listings": (), "last_failure": None, "last_read_at": None}

    def read_if_due() -> None:
        import time

        now = time.monotonic()
        due = (
            state["last_read_at"] is None
            or now - state["last_read_at"] >= refresh_interval_seconds
        )
        if not due:
            return
        try:
            state["listings"] = fetch_and_parse_listings(adapter)
            state["last_failure"] = None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            state["last_failure"] = f"{type(failure).__name__}: {failure}"
            return
        state["last_read_at"] = now
        publish_listings(state["listings"])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=read_if_due,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(state["listings"], state["last_failure"]),
    )
```

- [ ] **Step 4: Run Step 1's test, confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_instrument_catalogue_reader.py -v
```

- [ ] **Step 5: Add the settings entry**

```toml
[broker_catalogue_refresh_interval]
value = 3600
unit  = "seconds"
note  = "Claude, 2026-09-01: how often broker-instrument-catalogue-reader re-fetches Upstox's instrument master. Upstox states the files refresh once daily around 6 AM IST and only rarely intraday (spec section 4) -- hourly is generous headroom against that without hammering a static-asset URL 24 times more than the file ever actually changes."
```

- [ ] **Step 6: Commit**

```bash
git add parts/broker_adapter/broker_instrument_catalogue_reader.py tests/parts/broker_adapter/test_broker_instrument_catalogue_reader.py
git commit -m "feat: broker-instrument-catalogue-reader part

Fetches Upstox's daily gzipped instrument files -- no cursor loop, these
are whole files refreshed once a day, not a paginated catalogue the way
the crypto build's symbol-catalogue-reader read Binance/Bybit's."
```

---

### Task 5: `broker-market-feed-reader`

**Files:**
- Create: `parts/broker_adapter/broker_market_feed_reader.py`
- Test: `tests/parts/broker_adapter/test_broker_market_feed_reader.py`
- Modify: `runtime/brokers/upstox.py` (implement `encode_subscribe_frame`/`encode_unsubscribe_frame`, deferred in Task 2)

**Interfaces:**
- Consumes: `broker-token-standing` (Task 3), `broker-instrument-listing` (Task 4)
- Produces: `broker-market-data`, `broker-candle`, `broker-order-book-snapshot`, `broker-open-interest`, `broker-option-greeks`

- [ ] **Step 1: Resolve the subscribe-frame format left open in Task 2**

The spec (`docs/superpowers/specs/2026-09-01-upstox-adapter-design.md` §5) states the docs page shows a JSON body (`{"guid", "method", "data": {"mode", "instrumentKeys"}}`) but says it must be sent **binary**. The `.proto` schema committed in Task 2 defines only the *response* (`FeedResponse`) — it has no request message. Read `runtime/brokers/upstox_market_data_feed.proto` again to confirm this (it does — only response-shaped messages are defined). This means the subscribe request is UTF-8 JSON bytes sent over a binary WebSocket frame, not a second protobuf message — "binary format" in the docs describes the WebSocket frame type, not a second encoding.

```python
    def encode_subscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        return self._encode_frame("sub", requests)

    def encode_unsubscribe_frame(self, requests: Sequence[SubscriptionRequest]) -> bytes:
        return self._encode_frame("unsub", requests)

    @staticmethod
    def _encode_frame(method: str, requests: Sequence[SubscriptionRequest]) -> bytes:
        import json
        import uuid

        by_mode: dict[str, list[str]] = {}
        for request in requests:
            by_mode.setdefault(request.mode.value, []).append(request.instrument_key)
        # One frame per mode -- the documented request shape carries one
        # "mode" per message (spec section 5's sample), so a caller asking
        # for two modes at once is encoded as two frames, never guessed into one.
        if len(by_mode) != 1:
            raise ValueError(
                f"encode_subscribe_frame got {len(by_mode)} distinct modes in one "
                f"call; the documented request shape carries exactly one mode per "
                f"frame, so the caller must send one frame per mode."
            )
        [(mode, instrument_keys)] = by_mode.items()
        frame = {
            "guid": str(uuid.uuid4()),
            "method": method,
            "data": {"mode": mode, "instrumentKeys": instrument_keys},
        }
        return json.dumps(frame).encode("utf-8")
```

- [ ] **Step 2: Write the failing test for the frame encoding**

```python
# tests/runtime/brokers/test_upstox.py (append)
import json


def test_encode_subscribe_frame_is_json_bytes_with_one_mode():
    adapter = UpstoxAdapter()
    frame = adapter.encode_subscribe_frame([
        SubscriptionRequest(instrument_key="NSE_EQ|INE002A01018", mode=SubscriptionMode.FULL),
    ])
    decoded = json.loads(frame.decode("utf-8"))
    assert decoded["method"] == "sub"
    assert decoded["data"]["mode"] == "full_d5"
    assert decoded["data"]["instrumentKeys"] == ["NSE_EQ|INE002A01018"]
    assert "guid" in decoded


def test_encode_subscribe_frame_refuses_mixed_modes_in_one_call():
    import pytest

    adapter = UpstoxAdapter()
    with pytest.raises(ValueError):
        adapter.encode_subscribe_frame([
            SubscriptionRequest(instrument_key="A", mode=SubscriptionMode.FULL),
            SubscriptionRequest(instrument_key="B", mode=SubscriptionMode.LTPC),
        ])
```

(Add the needed imports — `SubscriptionMode`, `SubscriptionRequest` — to the top of `tests/runtime/brokers/test_upstox.py` if not already there from Task 2.)

- [ ] **Step 3: Run it, confirm it fails, then implement Step 1's code in `runtime/brokers/upstox.py`, then confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/runtime/brokers/test_upstox.py -v
```

- [ ] **Step 4: Write the failing test for the part's plan-building logic — which instruments get subscribed, respecting `does_subscription_fit_connection`**

```python
# tests/parts/broker_adapter/test_broker_market_feed_reader.py
from parts.broker_adapter.broker_market_feed_reader import plan_subscriptions
from runtime.brokers.broker_adapter import InstrumentListing, SubscriptionMode
from runtime.brokers.upstox import UpstoxAdapter


def _listing(key: str) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_EQ", instrument_type="EQ",
        trading_symbol=key, lot_size=1, tick_size=0.05, freeze_quantity=None,
        expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )


def test_plan_subscriptions_stops_at_the_full_mode_individual_limit():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(2500))  # over the 2000 individual cap
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 2000


def test_plan_subscriptions_covers_every_listing_when_under_the_cap():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(180))
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 180
```

- [ ] **Step 5: Run it, confirm it fails**

- [ ] **Step 6: Implement the part**

```python
# parts/broker_adapter/broker_market_feed_reader.py
"""broker-market-feed-reader: stream a broker's feed, decomposed into this
project's own record kinds (spec section 5).

WebSocket connection, protobuf-decoded via the adapter, one bundled message
per instrument split into up to five separate record kinds before
publishing -- never republished as one wire carrying several shapes
(docs/proposals/upstox-broker-adapter.md, same reasoning that split
candle/market-data/order-book-snapshot apart for the crypto build).
"""

from __future__ import annotations

from typing import Sequence

from runtime.brokers.broker_adapter import (
    BrokerAdapter, InstrumentListing, SubscriptionMode, SubscriptionRequest,
)
from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-market-feed-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("broker-token-standing", "broker-instrument-listing"),
    produces=(
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks", "part-health",
    ),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


def plan_subscriptions(
    adapter: BrokerAdapter,
    listings: Sequence[InstrumentListing],
    mode: SubscriptionMode,
) -> tuple[SubscriptionRequest, ...]:
    """As many listings as fit one connection at this mode, in listing order.

    Stops rather than errors at the cap -- a universe larger than one
    connection's limit is real (spec section 5's option-chain-width open
    item) and this plans what fits, leaving what doesn't for a second
    connection a later pass adds, not a crash now."""
    accepted: list[SubscriptionRequest] = []
    for listing in listings:
        candidate = SubscriptionRequest(instrument_key=listing.instrument_key, mode=mode)
        if adapter.does_subscription_fit_connection(tuple(accepted), candidate):
            accepted.append(candidate)
        else:
            break
    return tuple(accepted)


def describe_standing(counts: dict, last_failure: str | None) -> dict:
    return {"part_id": PART_ID, **counts, "last_failure": last_failure}


def start_part(context) -> int:
    """Opens one connection, subscribes to what the current instrument
    listing + token standing allow, decodes and republishes every message.

    The actual WebSocket I/O (open, read loop, reconnect-with-backoff) is
    the part of this file every existing crypto reader (venue_trade_stream_
    reader.py) already solved once for a near-identical shape -- follow that
    file's connection-loop pattern rather than re-deriving one here; this
    task's tests cover the pieces that are genuinely new (plan_subscriptions,
    the decode path), not the socket loop itself.
    """
    import time

    from runtime.brokers.upstox import UpstoxAdapter
    from runtime.input_assembly import LatestByKey

    adapter = UpstoxAdapter()
    token_standing = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    instrument_listings = LatestByKey(
        read=context.bus.reader("broker-instrument-listing"),
        key_of=lambda _: adapter.broker_id,
        maximum_age_seconds=context.number("broker_instrument_listing_maximum_age"),
    )

    publish_ltp = context.bus.publisher_for("broker-market-data")
    publish_candle = context.bus.publisher_for("broker-candle")
    publish_book = context.bus.publisher_for("broker-order-book-snapshot")
    publish_oi = context.bus.publisher_for("broker-open-interest")
    publish_greeks = context.bus.publisher_for("broker-option-greeks")

    counts = {"decoded_messages": 0, "last_failure": None}

    def on_message(payload: bytes) -> None:
        try:
            decoded = adapter.decode_feed_message(payload)
        except Exception as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            return
        counts["decoded_messages"] += 1
        for update in decoded.ltp_updates:
            publish_ltp(update)
        for candle in decoded.candles:
            publish_candle(candle)
        for book in decoded.book_updates:
            publish_book(book)
        for oi in decoded.open_interest:
            publish_oi(oi)
        for greeks in decoded.option_greeks:
            publish_greeks(greeks)

    from websockets.exceptions import ConnectionClosed, WebSocketException
    from websockets.sync.client import connect as connect_websocket

    state = {
        "connection": None,
        "subscribed": (),
        "backoff_seconds": context.number("broker_reconnect_backoff_floor"),
    }
    counts["last_failure"] = None

    def ensure_connected() -> bool:
        if state["connection"] is not None:
            return True
        token = token_standing.mapping().get(adapter.broker_id)
        listings = instrument_listings.mapping().get(adapter.broker_id)
        if token is None or not token.is_still_valid() or not listings:
            # Not a failure -- a normal state before either producer has
            # spoken, or after the token has expired and refresh is still
            # in flight. Reported on the standing, never raised.
            return False
        plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
        if not plan:
            return False
        try:
            connection = connect_websocket(
                adapter.stream_endpoint_url(),
                additional_headers={
                    "Authorization": f"Bearer {token.access_token}",
                    "Accept": "*/*",
                },
                open_timeout=context.number("broker_connection_open_timeout"),
            )
            connection.send(adapter.encode_subscribe_frame(plan))
        except (OSError, WebSocketException) as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            return False
        state["connection"] = connection
        state["subscribed"] = plan
        state["backoff_seconds"] = context.number("broker_reconnect_backoff_floor")
        return True

    def drain_one_tick() -> None:
        if not ensure_connected():
            return
        connection = state["connection"]
        deadline = time.monotonic() + context.number("broker_stream_drain_interval")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                message = connection.recv(timeout=remaining)
                if isinstance(message, str):
                    continue  # a text frame is never a feed message -- binary only
                on_message(message)
        except TimeoutError:
            return  # nothing arrived this drain window -- not a fault
        except (ConnectionClosed, WebSocketException, OSError) as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"
            connection.close()
            state["connection"] = None
            ceiling = context.number("broker_reconnect_backoff_ceiling")
            state["backoff_seconds"] = min(state["backoff_seconds"] * 2, ceiling)
            time.sleep(state["backoff_seconds"])

    def describe_standing() -> dict:
        return {
            "part_id": PART_ID,
            "connected": state["connection"] is not None,
            "subscribed_instruments": len(state["subscribed"]),
            "decoded_messages": counts["decoded_messages"],
            "last_failure": counts["last_failure"],
        }

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=drain_one_tick,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=describe_standing,
        )
    finally:
        if state["connection"] is not None:
            state["connection"].close()
```

- [ ] **Step 7: Run Step 4's test, confirm it passes**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_market_feed_reader.py -v
```

- [ ] **Step 8: Add the two settings entries this part reads**

```toml
[broker_token_standing_maximum_age]
value = 600
unit  = "seconds"
note  = "Claude, 2026-09-01: how old a broker-token-standing reading may be before broker-market-feed-reader stops trusting it. Every LatestByKey needs its keys to expire (2026-08-26 finding: a level with no age bound never lets a stopped producer be noticed) -- ten minutes is twice broker_token_refresh_check_interval (300s), so two missed refresh cycles must pass before this part treats the token as gone rather than merely due."

[broker_instrument_listing_maximum_age]
value = 7200
unit  = "seconds"
note  = "Claude, 2026-09-01: how old a broker-instrument-listing reading may be before broker-market-feed-reader stops trusting it. Twice broker_catalogue_refresh_interval (3600s), same reasoning as broker_token_standing_maximum_age -- one missed catalogue read is not yet a fault, two in a row is."

[broker_reconnect_backoff_floor]
value = 1.0
unit  = "seconds"
note  = "Claude, 2026-09-01: the first wait after a dropped Upstox feed connection, before doubling on repeated failures. Matches venue_reconnect_backoff_floor's own starting point for the crypto readers -- no measurement yet exists for Upstox's own reconnect behaviour, so this borrows the crypto build's already-justified floor rather than inventing an unmeasured one."

[broker_reconnect_backoff_ceiling]
value = 60.0
unit  = "seconds"
note  = "Claude, 2026-09-01: the most this part waits between reconnect attempts, however many have failed in a row. Same reasoning and same value as venue_reconnect_backoff_ceiling -- a minute is long enough to stop hammering a broker that is genuinely down, short enough that a market-hours outage is not compounded by the reader itself sleeping through the recovery."

[broker_stream_drain_interval]
value = 1.0
unit  = "seconds"
note  = "Claude, 2026-09-01: the longest broker-market-feed-reader blocks on one socket read before yielding back to the tick loop. T-2 -- a part that blocked longer than this on I/O is a part the governor cannot switch promptly. One second matches stream_drain_interval's own value for the crypto readers."

[broker_connection_open_timeout]
value = 10.0
unit  = "seconds"
note  = "Claude, 2026-09-01: how long to wait for the Upstox WebSocket handshake before treating it as failed. No Upstox-specific measurement yet -- ten seconds is a conservative default pending one, generous enough for a slow network without leaving a part hung well past its health-report interval."
```

- [ ] **Step 9: Run everything in Task 5, confirm it still passes**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_market_feed_reader.py tests/runtime/brokers/test_upstox.py -v
```

`ensure_connected`/`drain_one_tick` inside `start_part` are not independently unit-tested — matching this codebase's own precedent (`symbol_catalogue_reader.py`'s `start_part` has no direct test either; only the pure functions it wires together do). `plan_subscriptions` (Step 4) and `decode_feed_message`/`encode_subscribe_frame` (Task 2) already cover every piece of this task's logic that is genuinely testable without a live socket.

- [ ] **Step 10: Commit**

```bash
git add parts/broker_adapter/broker_market_feed_reader.py tests/parts/broker_adapter/test_broker_market_feed_reader.py runtime/brokers/upstox.py tests/runtime/brokers/test_upstox.py
git commit -m "feat: broker-market-feed-reader, full connection loop

plan_subscriptions and the decode-and-decompose path are tested directly.
The WebSocket connection itself uses websockets.sync.client (already a
pinned dependency), with reconnect/backoff settings borrowed from the
crypto readers' own already-justified values pending an Upstox-specific
measurement. Tape-writing is deliberately not this part's job -- it
publishes decomposed records to the bus, broker-market-tape-writer (Task
6) is what persists them."
```

---

### Task 6: `broker-market-tape-writer`

**Files:**
- Create: `parts/broker_adapter/broker_market_tape_writer.py`
- Test: `tests/parts/broker_adapter/test_broker_market_tape_writer.py`

**Interfaces:**
- Consumes: `broker-market-data`, `broker-candle`, `broker-order-book-snapshot`, `broker-open-interest`, `broker-option-greeks` (all from Task 5)
- Produces: nothing but `part-health` (per `docs/features.json`'s declaration)

- [ ] **Step 1: The shared tape-writing machinery this task calls into**

Already read in full while writing this plan (`runtime/tape.py`'s `TapeWriter`, `parts/market_data_feed/venue_trade_stream_reader.py`). The relevant facts:

`TapeWriter(root, venue, symbol, writeback_interval_bytes, stream_kind)` — **one writer per (venue-or-broker-id, symbol, stream_kind) triple**; its `.append(stream_kind, payload, received_at_ns=None, venue_time_ns=NOT_SENT, sequence=NOT_SENT)` refuses (`TapeKindRefused`) a payload whose kind doesn't match the writer's own — one file, one kind, enforced by construction, after 2026-08-25's two-writers-on-one-blob incident.

**One real divergence from the crypto reader worth stating plainly rather than copying silently:** `venue_trade_stream_reader.py` writes the *raw venue payload* to the tape and normalises only on read ("the tape keeps the venue's bytes"), because each raw Binance/Bybit message already carries exactly one `StreamKind`. Upstox's feed doesn't have that property — one raw protobuf message can bundle LTP, book, OHLC, open interest and greeks together (Task 2's `decode_feed_message`), and the blueprint (`docs/features.json`, `docs/proposals/upstox-broker-adapter.md`) already commits `broker-market-tape-writer` to consuming the five *decomposed* bus types, not a raw payload — there is no `broker-raw-feed-message` data type declared for it to consume instead. So this part's `payload` is a JSON encoding of the already-normalised record it received, not the broker's raw bytes. Named here so it reads as a deliberate, reasoned choice forced by Upstox's message shape, not an oversight against the crypto pattern.

**Also worth flagging, not solving in this task:** at up to ~200 symbols × 5 stream kinds, this part can hold up to ~1000 concurrently open `TapeWriter`s (each with two open file handles, matching `symbol_catalogue_reader.py`'s own precedent of naming a file-descriptor ceiling as a real concern rather than discovering it in production). `TapeWriter`'s own `_writer_for`-style laziness (open only once a symbol's first message of that kind actually arrives, matching `StreamTapeRecorder._writer_for`) keeps this to what is actually captured rather than the full planned universe, which is implemented below -- but the ceiling itself is not measured here and should be before this runs against the full F&O-eligible universe.

- [ ] **Step 2: Write the failing test for routing each broker record kind to its `StreamKind`**

```python
# tests/parts/broker_adapter/test_broker_market_tape_writer.py
from parts.broker_adapter.broker_market_tape_writer import stream_kind_for
from runtime.brokers.broker_adapter import (
    BrokerCandle, BrokerOpenInterest, BrokerOptionGreeks,
    BrokerOrderBookUpdate, LtpUpdate,
)
from runtime.tape import StreamKind


def test_stream_kind_for_each_broker_record_type():
    assert stream_kind_for(LtpUpdate) == StreamKind.TRADE
    assert stream_kind_for(BrokerCandle) == StreamKind.CANDLE
    assert stream_kind_for(BrokerOrderBookUpdate) == StreamKind.BOOK
    assert stream_kind_for(BrokerOpenInterest) == StreamKind.OPEN_INTEREST
    assert stream_kind_for(BrokerOptionGreeks) == StreamKind.OPTION_GREEKS
```

- [ ] **Step 3: Run it, confirm it fails**

- [ ] **Step 4: Implement `stream_kind_for` and the part**

```python
# parts/broker_adapter/broker_market_tape_writer.py
"""broker-market-tape-writer: persist every decomposed broker feed record to
the tape before anything else reads it.

History accrues only in real time for this segment too -- same reasoning
that made market-data-feed the crypto build's first vertical (RL-068).
Routes each broker record type to the StreamKind Task 1 added; the tape
format itself (runtime/tape.py) is unmodified shared substrate.
"""

from __future__ import annotations

from runtime.brokers.broker_adapter import (
    BrokerCandle, BrokerOpenInterest, BrokerOptionGreeks,
    BrokerOrderBookUpdate, LtpUpdate,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import StreamKind

PART_ID = "broker-market-tape-writer"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(
        "broker-market-data", "broker-candle", "broker-order-book-snapshot",
        "broker-open-interest", "broker-option-greeks",
    ),
    produces=("part-health",),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

_KIND_BY_TYPE = {
    LtpUpdate: StreamKind.TRADE,
    BrokerCandle: StreamKind.CANDLE,
    BrokerOrderBookUpdate: StreamKind.BOOK,
    BrokerOpenInterest: StreamKind.OPEN_INTEREST,
    BrokerOptionGreeks: StreamKind.OPTION_GREEKS,
}


def stream_kind_for(record_type: type) -> StreamKind:
    return _KIND_BY_TYPE[record_type]


def _payload_for(record) -> bytes:
    """JSON encoding of this project's own normalised record -- not the
    broker's raw bytes (Step 1's note on why that differs from the crypto
    tape's own convention)."""
    import dataclasses
    import json

    return json.dumps(dataclasses.asdict(record), default=str).encode("utf-8")


def start_part(context) -> int:
    """One TapeWriter per (instrument_key, StreamKind), opened lazily on
    first message -- same discipline as StreamTapeRecorder._writer_for, so a
    symbol that never sends one of the five kinds never gets a hollow empty
    tape for it (Step 1's file-descriptor note)."""
    import pathlib

    from runtime.tape import TapeWriter

    settings = context.settings[RUNTIME_SCOPE_NAME]
    tape_root = pathlib.Path(str(settings.entries["tape_root"].value)).expanduser()
    writeback_interval_bytes = int(context.number("writeback_interval"))

    writers: dict[tuple[str, "StreamKind"], TapeWriter] = {}
    counts = {"records_written": 0, "last_failure": None}

    def writer_for(instrument_key: str, kind) -> TapeWriter:
        key = (instrument_key, kind)
        writer = writers.get(key)
        if writer is None:
            writer = TapeWriter(
                tape_root, "upstox", instrument_key, writeback_interval_bytes,
                stream_kind=kind,
            )
            writers[key] = writer
        return writer

    def write_one(record) -> None:
        kind = stream_kind_for(type(record))
        writer_for(record.instrument_key, kind).append(
            stream_kind=kind,
            payload=_payload_for(record),
            venue_time_ns=record.broker_time_ns,
        )
        counts["records_written"] += 1

    readers = tuple(
        context.bus.reader(data_type)
        for data_type in (
            "broker-market-data", "broker-candle", "broker-order-book-snapshot",
            "broker-open-interest", "broker-option-greeks",
        )
    )

    def write_all_pending() -> None:
        try:
            for read in readers:
                for message in read():
                    write_one(message.payload)
            counts["last_failure"] = None
        except OSError as failure:
            counts["last_failure"] = f"{type(failure).__name__}: {failure}"

    def describe_standing() -> dict:
        return {"part_id": PART_ID, "open_tapes": len(writers), **counts}

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=write_all_pending,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=describe_standing,
        )
    finally:
        for writer in writers.values():
            writer.close()
```

- [ ] **Step 5: Write the failing test for `_payload_for`**

```python
def test_payload_for_json_encodes_the_record():
    import json
    from parts.broker_adapter.broker_market_tape_writer import _payload_for

    update = LtpUpdate(
        instrument_key="NSE_EQ|INE002A01018", last_traded_price=1234.5,
        last_traded_quantity=10.0, last_traded_time_ms=1740729552723,
        close_price=1230.0, broker_time_ns=1740729566039_000_000,
    )
    decoded = json.loads(_payload_for(update))
    assert decoded["instrument_key"] == "NSE_EQ|INE002A01018"
    assert decoded["last_traded_price"] == 1234.5
```

- [ ] **Step 6: Run Steps 2 and 5's tests, confirm everything passes**

```bash
.venv/bin/python3 -m pytest tests/parts/broker_adapter/test_broker_market_tape_writer.py -v
```

- [ ] **Step 7: Commit**

```bash
git add parts/broker_adapter/broker_market_tape_writer.py tests/parts/broker_adapter/test_broker_market_tape_writer.py
git commit -m "feat: broker-market-tape-writer

Routes each of the five decomposed record types to its own StreamKind-
tagged TapeWriter, lazily opened per instrument. Payload is a JSON
encoding of the already-normalised record, not raw broker bytes --
documented as a deliberate divergence from the crypto tape's raw-bytes
convention, forced by Upstox's bundled per-instrument feed message
having no single StreamKind of its own to keep raw."
```

---

## What this plan deliberately does not finish

Order placement and margin reading are not in this plan at all — `docs/proposals/upstox-broker-adapter.md` already explains why: no live-order consumer exists yet to wire them into, and RL-062's no-placeholder discipline applies to a plan as much as to a blueprint edit.

Two smaller, explicitly named gaps inside what the plan does build:

- **`ensure_connected`/`drain_one_tick` (Task 5) are not unit-tested directly.** They're integration glue over a real socket library; `plan_subscriptions`, `decode_feed_message`, and `encode_subscribe_frame` — the actual decision logic they call into — are. This matches `symbol_catalogue_reader.py`'s own precedent: its `start_part` has no direct test either, only the pure functions it wires together do.
- **The ~1000-open-file-handle ceiling (Task 6) is named, not measured.** At the full F&O-eligible universe (~200 symbols) across 5 stream kinds, worth a real measurement before this runs unattended against that many instruments at once — flagged the same way `symbol_catalogue_reader.py` flags its own file-descriptor concern (spec §4.2) rather than silently assumed fine.

Both `extended_token`'s unexplained validity window and the exact option-chain width against Upstox's subscription caps (spec §8) remain open from the spec itself — neither blocks this plan's four parts, both matter before this runs against the full nearest-expiry options chain.
