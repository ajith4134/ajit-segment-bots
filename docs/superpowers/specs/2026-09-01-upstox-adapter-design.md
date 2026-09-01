# Technical spec — Upstox broker adapter

Status: draft, awaiting user review. First of six broker adapters
(`docs/goal.md` §6): Upstox → Zerodha → Angel One → Fyers → ICICI Breeze →
Groww. Primary for paper trading; the other five are added later for data
redundancy and rate-limit headroom, not parallel capital (per user, 2026-09-01).

Facts below were verified against Upstox's own developer docs on 2026-09-01
(`upstox.com/developer/api-documentation/...`), not recalled from training —
this project's RL-061/RL-063 standard applies to venue facts as much as to
settings. Anything not directly confirmed is marked **UNVERIFIED** and deferred
to implementation time, never assumed.

---

## 1. Why the old `VenueAdapter` contract is retired, not extended

`runtime/venues/venue_adapter.py`'s `VenueAdapter` is shaped for crypto
perpetual futures: `ContractFunding`, `VenuePremium`, `read_funding_facts`,
`margin_schedule_requests`, a `PREMIUM` stream kind. None of that exists for
NSE/BSE/MCX cash and derivatives — there is no funding rate, no perpetual mark
price. Confirmed by the user (2026-09-01): since crypto is fully retired, the
old file is deleted (git history keeps it, Rule 9 — nothing is lost) rather
than split or extended. A fresh contract is built at `runtime/brokers/`,
Indian-market-shaped from the start, so none of the six new adapters ever
implements a method that means nothing in their domain.

The genuinely universal ideas — trade/candle/book/quote normalisation, a ban
signal, connection/heartbeat discipline, a fact-with-provenance pattern
(`VenueFact` → `BrokerFact`, same RL-061 reasoning) — are **patterns to
reproduce**, not code to inherit. `runtime/venues/` is not imported by
anything under `runtime/brokers/`.

## 2. What Upstox actually is, structurally

Four separate concerns, each with its own endpoint family and its own shape.
No single "adapter interface" question set covers all four the way crypto's
did, because Upstox's own API doesn't either:

| Concern | Endpoint family | Shape |
|---|---|---|
| Auth | `api.upstox.com/v2/login/*` | OAuth2 authorization-code, daily-expiring token |
| Instrument discovery | `assets.upstox.com/market-quote/instruments/*` | Static gzipped JSON files, refreshed once a day |
| Market data | `wss://.../feed/market-data-feed` (v3) | WebSocket, protobuf-encoded, one bundled message per instrument |
| Orders & margin | `api-hft.upstox.com/v2/order/*`, `api.upstox.com/v2/charges/margin` | REST, JSON |

## 3. Auth and the daily token expiry — the operational fact that shapes everything else

**Confirmed**: `access_token` is valid until **3:30 AM the next day**,
regardless of when it was generated (`get-token` docs). There is no refresh
token in the standard flow. Three ways to get a token:

1. **Authorization-code flow** — a customer clicks through a login page.
   Requires a human in the loop.
2. **Semi-automated** — the app triggers an auth request at a scheduled time;
   a human still approves it (click a notification or visit the developer
   dashboard); the token is then pushed to a notifier URL the app listens on.
3. **Manual** — copy a token from the developer dashboard by hand.

**None of the three is a fully unattended daily re-auth.** Every path needs a
human action once every 24 hours before 3:30 AM, or the spine trades on a dead
token starting that morning. This is the single biggest fact this spec
surfaces for the user's decision (§8) — it is not solvable by better code, it
is a property of Upstox's auth model.

`extended_token` also appears in the token response, unexplained by the pages
fetched — **UNVERIFIED** what its own validity window is or what it's scoped
to (possibly a longer-lived, read-only/market-data-only credential). Worth
checking before implementation; if it does extend market-data access beyond
3:30 AM it changes how much of the spine actually stops each night.

## 4. Instrument discovery — a daily file, not a paginated catalogue

Crypto's `catalogue_url`/`read_catalogue_cursor` pattern (paginated REST,
cursor-driven) doesn't apply. Upstox publishes **static gzipped JSON files**,
one per exchange, refreshed once a day around 6 AM IST:

    https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz
    https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
    https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz
    https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz

One record per listing, `instrument_key` is the identifier to key everything
else off (`exchange_token` is reused by the exchange after a contract expires,
so it is not stable — same caution the crypto adapter already applies to
venue-native symbols). Shape varies by `instrument_type`:

- **Equity** (`EQ`): `instrument_key` = `NSE_EQ|<ISIN>`, carries `lot_size`
  (usually 1), `tick_size`, `freeze_quantity`. The MIS-specific file adds
  `intraday_margin` and `intraday_leverage` per symbol — this is the actual
  leverage figure for the "leverage stocks buy" part of the goal, sourced from
  the venue rather than assumed (RL-061).
- **Futures** (`FUT`): `instrument_key` = `NSE_FO|<exchange_token>` (numeric,
  not compositional), `expiry` (epoch ms), `lot_size`, `underlying_key`.
- **Options** (`CE`/`PE`): same shape as futures plus `strike_price`.
- **Index** (`INDEX`): no `lot_size`/`tick_size` — indices aren't traded
  directly, only their derivatives are.

`BrokerAdapter.instrument_listing_urls()` returns this fixed set of URLs
instead of a paginated request loop; `read_instrument_listings(response)`
parses the (already-fetched, gunzipped) JSON array. A truncated download is
detectable by the file's own record count being far off the previous day's,
the same "a truncated universe is the worst kind of wrong" concern the crypto
adapter's paging design already worried about — worth carrying forward as a
sanity check, not a paging contract.

## 5. Market data feed — one bundled message per instrument, not four separate streams

This is the structural difference from crypto that the new contract has to
absorb, not paper over.

Binance/Bybit stream **trade**, **candle**, **book**, and **quote** as four
separate topics a reader subscribes to independently, and each pushes its own
message shape. Upstox's v3 feed subscribes by `instrumentKey` + a **mode**
(`ltpc`, `option_greeks`, `full`, `full_d30`), and each mode's message bundles
several of those concepts **together in one payload per instrument**:

```
"full" feed for one instrument, one message:
  ltpc         { ltp, ltt, ltq, cp }          -- last-traded price info
  marketLevel  { bidAskQuote[] }               -- up to 5 (or 30 on full_d30) book levels
  marketOHLC   { ohlc: [{1d bar}, {current-minute bar}] }
  optionGreeks { delta, theta, gamma, vega, rho }   -- options only
  oi, iv, atp, vtt, tbq, tsq                   -- open interest, implied vol, etc.
```

So `decode_feed_message(payload: bytes) -> FeedUpdate` returns one structured
record per instrument, and **the reader decomposes it into tape records**,
not the adapter. Two consequences for the tape's `StreamKind` vocabulary
(`runtime/tape.py`):

- **LTP is not a trade print.** Upstox's retail market-data API states the
  last-traded price the exchange reported; it does not stream every
  individual print the way Binance/Bybit do. Recording it as `TRADE` would
  claim a resolution this feed does not have — the same reasoning that put
  `TradeFidelity` on `NormalisedTrade` in the first place. **New value
  needed**: `TradeFidelity.LAST_TRADED_PRICE_ONLY` — an LTP update, not an
  executed-print stream. `ltq` (last-traded quantity) travels with it but is
  the exchange's own last-print size, not a venue-assigned trade id or
  sequence — there is no trade-level sequence number in this feed at all
  (**UNVERIFIED against the actual `.proto` schema** — the JSON sample shown
  in the docs is illustrative of field names, not the wire encoding itself;
  confirm against `MarketDataFeed.proto` before implementing `decode_feed_message`).
- **Open interest and option Greeks have no crypto equivalent.** New
  `StreamKind` values: `OPEN_INTEREST` (oi, vtt, tbq, tsq — the aggregate
  demand/supply figures unique to NSE derivatives) and `OPTION_GREEKS` (delta,
  theta, gamma, vega, rho, iv) — both stored as their own tape records so a
  consumer of one doesn't have to parse a message shaped for the other,
  matching why `PREMIUM` got its own kind in the crypto tape rather than being
  folded into `TRADE`.

`marketOHLC`'s two bars (`1d` and the current-minute `I1`) map onto the
existing `CANDLE` kind directly — `is_closed` is true for `1d` only during
that day's own close, and Upstox's `I1` label is a fact to carry as
`interval`, not translate.

**Binary, not JSON.** The docs state the market_info/snapshot/live_feed
samples in JSON for readability, but the wire format is Protobuf — decoding
needs the published `.proto` schema
(`assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto`), not a
JSON parser. **Fetch and read that schema before writing
`decode_feed_message` — the JSON samples in this spec describe field
semantics, not the actual bytes on the wire.**

**Connection and subscription limits** (free tier, confirmed):

| | Individual | Combined |
|---|---|---|
| Connections | 2 per user | — |
| LTPC | 5000 instrument keys | 2000 |
| Option Greeks | 3000 | 2000 |
| Full | 2000 | 1500 |

"Combined" applies once a connection subscribes to more than one mode. Against
a ~180-200 symbol F&O-eligible universe (§ goal.md build order) this is not
tight for equity+futures alone, but index options at multiple strikes across
a nearest-expiry chain can run into hundreds of instrument keys per
underlying — sizing this against the real option-chain width per index is
implementation work, not something to guess at spec time.

`does_subscription_fit_connection` (the Upstox analogue of crypto's
`does_topic_fit_connection`) has to check **both** the individual mode cap and
the combined cap across whatever modes are already open on that connection —
a strictly harder question than either crypto venue asked, since neither of
them had a cross-category combined limit.

**Heartbeat**: standard WS ping/pong, handled by most client libraries
automatically — simpler than Bybit's app-level ping requirement.

## 6. Orders and margin

REST, JSON, on a **separate low-latency host** from the rest of the API
(`api-hft.upstox.com` for order placement vs `api.upstox.com` for
everything else) — worth preserving as a fact the adapter states rather than
a URL a reader hardcodes.

**Order fields** (`POST /v2/order/place`): `instrument_token`, `quantity`,
`product` (`I` intraday, `D` delivery, `MTF` margin-trading-facility — no
plain `CNC` string, delivery is `D`), `order_type`
(`MARKET`/`LIMIT`/`SL`/`SL-M`), `transaction_type` (`BUY`/`SELL`), `validity`
(`DAY`/`IOC`), `price`, `trigger_price`, `disclosed_quantity`, `is_amo`
(ignored during market hours — Upstox infers AMO itself), `market_protection`
(a percentage band on market orders, `-1` = exchange default).

**`X-Algo-Name` header** — optional unless the account has an
exchange-approved algo strategy, in which case it must match a configured
algo name. This is the concrete mechanism behind the SEBI algo-ID requirement
flagged in `docs/goal.md` — not needed for paper trading, required the day
this adapter starts placing live orders.

**Margin** (`POST /v2/charges/margin`, max 20 instruments per call): returns
`span_margin`, `exposure_margin`, `equity_margin`, `net_buy_premium`,
`additional_margin` per instrument — the real SEBI SPAN+exposure figures, not
a flat leverage assumption. This is what should price a position's capital
requirement, the same way the crypto build priced funding from the venue's
own numbers rather than a platform default (RL-061).

## 7. Contract sketch — `runtime/brokers/broker_adapter.py`

Not final code, the shape the spec is proposing:

```
class BrokerFact         # value, unit, source — same discipline as VenueFact
class InstrumentListing  # instrument_key, exchange, segment, instrument_type,
                          # lot_size, tick_size, freeze_quantity, expiry,
                          # strike_price, option_type, underlying_key,
                          # intraday_margin, intraday_leverage (all optional
                          # except instrument_key/exchange/segment/instrument_type,
                          # per-instrument-type as Upstox's own files vary)
class FeedUpdate          # decomposed per-kind: LtpUpdate, BookUpdate (reused
                          # shape), OhlcUpdate (-> CANDLE), OpenInterestUpdate,
                          # OptionGreeksUpdate
class MarginQuote         # span/exposure/equity/net_buy_premium/additional, per instrument
class OrderRequest / OrderResult   # the place-order fields above

class BrokerAdapter(abc.ABC):
    broker_id
    declared_limits() -> Mapping[str, BrokerFact]
    token_expiry_policy() -> TokenExpiryPolicy   # NEW concept, no crypto equivalent
    instrument_listing_urls() -> tuple[str, ...]
    read_instrument_listings(response) -> tuple[InstrumentListing, ...]
    stream_endpoint_url()
    heartbeat_discipline()
    connection_discipline()
    encode_subscribe_frame(requests) -> bytes     # protobuf for Upstox
    does_subscription_fit_connection(existing, candidate) -> bool  # individual + combined
    decode_feed_message(payload: bytes) -> FeedUpdate
    order_endpoint_url()
    build_order_request(...) -> OrderRequest
    read_order_result(response) -> OrderResult
    margin_endpoint_url()
    read_margin_quote(response) -> Mapping[str, MarginQuote]
    read_http_ban_signal(status_code, headers) -> BanSignal | None
```

`TokenExpiryPolicy` is the one genuinely new abstract concept versus the
crypto contract — it exists because Upstox's daily-expiry-with-human-approval
model has no crypto analogue (Binance/Bybit API keys don't expire daily), and
whatever this returns is what a future `broker-token-refresh-scheduler` part
would act on.

## 8. Open items — yours to decide before implementation starts

- **Daily re-auth**: semi-automated flow (scheduled request, you approve via
  notification/dashboard once a day) is the closest thing to automated Upstox
  offers. Fully unattended is not available in the standard flow. Confirm
  this is acceptable, or say if a different broker in the six should be
  primary specifically because it doesn't have this constraint (worth
  checking Zerodha/Angel One/Fyers token lifetimes before assuming they're
  better — not yet verified for any of the other five).
- **`extended_token`**: unexplained by the pages fetched. Worth checking
  before implementation — if it extends market-data access past 3:30 AM, that
  changes how much of the spine actually goes dark each night.
- **Secrets storage**: following `docs/secrets.md`'s existing pattern
  (sops+age, `~/.config/<project>/secrets.enc.yaml`) — this project's crypto
  keys lived in the shared `~/.config/trading/` store because they were the
  same real exchange accounts as `~/trading-system`. Upstox credentials are
  unrelated to that store. Recommend a new
  `~/.config/ajit-segment-bots/secrets.enc.yaml`, created fresh, holding
  `upstox: {client_id, client_secret}` and (daily) `access_token`. Confirm.
- **Option-chain width vs subscription caps**: sizing the nearest-expiry
  index-options chain against the 1500-2000 combined "Full" mode cap needs a
  real chain width (varies by index, by day), not a guess — defer to
  implementation, flagged here so it isn't silently assumed to fit.
- **`.proto` schema**: fetch and read `MarketDataFeed.proto` before writing
  `decode_feed_message` — this spec's message shapes come from the docs' JSON
  illustrations, not the wire format itself.

## 9. What this becomes in the blueprint

Mirrors the crypto pattern (`market-data-feed` block, `execution_venue_adapter`
parts) rather than inventing a new one: an `upstox-instrument-catalogue-reader`
(daily file fetch + parse), an `upstox-market-feed-reader` (WebSocket +
protobuf decode + tape write, one part per T-1), and later, when this segment
moves toward live orders, an `upstox-order-router` and `upstox-margin-reader`
peer to the crypto build's `ccxt_order_router.py` /
`venue_balance_reader.py` equivalents. None of this is declared in
`docs/features.json` yet — that's the next step, a blueprint edit
(`dashboard/blueprint_edits/`) once this spec is approved, per the project's
own rule that a design change is a blueprint edit first, code follows.
