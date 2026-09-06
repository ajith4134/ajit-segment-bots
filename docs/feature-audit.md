# The 29-feature audit — the standing ledger

**This file is the memory of the second temporary goal** (`docs/goal.md`,
2026-09-05): walk all 29 foundational features one at a time, prove data
actually moves on every connection each of their parts declares, convert every
part still shaped like crypto into its Indian-market equivalent, and say what
is missing — both features and parts.

The user asked for that goal to **stay active across every session until it is
achieved completely**. A goal that spans sessions needs a record that spans
sessions, or each session re-walks what the last one already walked. This is
that record. It is appended to, never rewritten, and a feature is marked walked
only when its section below names what was measured.

## How a feature is walked

    python3 dashboard/audit_feature_dataflow.py                  the 29, at a glance
    python3 dashboard/audit_feature_dataflow.py <feature-id>     one, part by part
    python3 dashboard/audit_feature_dataflow.py <id> --json      the same, machine-readable

The instrument reads each part's **own bus counters**, surfaced through health
into the heartbeat table, so a wire is CARRYING only when its producer really
published that type and its consumer really received it. `NOT CARRYING` (both
ends running, nothing has travelled) is kept apart from `NOT MEASURED` (an end
is off, so nothing was observed) — collapsing the two would report a system as
idle when nobody had looked at it (Rule 8).

Three things are asked of every feature, and all three are answered in its
section or it is not walked:

1. every part's `consumes` and `produces`, measured in both directions
2. every part still reasoning in crypto's terms, and what it was converted to
3. what the feature is missing — a part it needs, or a whole feature the 29 lack

**A counter is only as old as the process.** Every count resets when the spine
restarts, and a market-hours reading and a weekend reading are different
measurements of different things. Each section below stamps when it was taken
and whether the market was open.

## Where the walk stands

| | |
|---|---|
| features walked | **2 of 29** |
| parts declared | 373 (`broker-quote-bridge` added 2026-09-06 by this walk) |
| parts running (2026-09-06 04:56 UTC) | 322 |
| parts with no `start_part` at all | 25 — 24 of them `stock-market-news-data` |
| wires carrying | 3,806 of 5,843 (65.1%) |

Order of the walk is not the blueprint's order. It is Monday-first: the
features the three segment bots' first live paper trade actually runs through
come before the ones that learn from a trade that has not happened yet.

| # | feature | state |
|---|---|---|
| 1 | `market-data-feed` | **walked 2026-09-06** — 4 defects found and fixed (bridges verified publishing); `broker-quote-bridge` named as missing |
| 2 | `broker-adapter` | **walked 2026-09-06** — every Upstox REST call in the project was Cloudflare-blocked; 3 parts fixed, feature now fully carrying |
| 3 | `execution-venue-adapter` | not walked |
| 4 | `paper-live-trading` | not walked |
| 5 | `opportunity-scanner` | not walked |
| 6 | `segment-bot` | not walked |
| 7 | `risk-capital-allocation` | not walked |
| 8 | `portfolio-state` | not walked |
| 9 | `capital-desk` | not walked |
| 10 | `bull-bot` | not walked |
| 11 | `bear-bot` | not walked |
| 12 | `profit-tailgating-bot` | not walked |
| 13 | `prediction` | not walked |
| 14 | `ledger` | not walked |
| 15 | `observability` | not walked |
| 16 | `resource-governor` | not walked |
| 17 | `closed-trade-decoding` | not walked |
| 18 | `learning-loop` | not walked |
| 19 | `intelligence` | not walked |
| 20 | `knowledge` | not walked |
| 21 | `hypothesis` | not walked |
| 22 | `ai-brain` | not walked |
| 23 | `skills` | not walked |
| 24 | `backtesting` | not walked |
| 25 | `llm-foundation` | not walked |
| 26 | `llm-services` | not walked |
| 27 | `online-research` | not walked |
| 28 | `autonomous` | not walked |
| 29 | `stock-market-news-data` | not walked — 24 of its 29 parts have no code at all |

---

## 1. `market-data-feed` — walked 2026-09-06 (market closed, spine up 6 min)

23 parts, 13 running, 23 launchable. 202 wires carrying, 83 idle between running
parts, 254 not measured because an end is off.

### Two defects, both measured, both fixed

**The three broker bridges had never published a single message.** Every part
downstream of `market-data`, `candle` and `order-book-snapshot` was reading a
wire nothing had ever been put on:

    broker-market-data-bridge   received 2,000 broker-market-data          published 0 market-data
    broker-candle-bridge        received 1,878 broker-candle               published 0 candle
    broker-order-book-bridge    received 1,999 broker-order-book-snapshot  published 0 order-book-snapshot

with `instruments_resolved` at 2,000 in all three. All three resolve a
`trading_symbol` by `instrument_key` before republishing and returned `None`
when the key was not known **yet** — and then dropped the update. The feed
delivers its whole snapshot the instant the socket connects, while
`subscribed-instrument-listing-filter` restates the subscribed set over a 300 s
conveyor, so the burst always meets an empty map. All 1,157 instrument keys on
that day's tape resolve in the instrument master, so nothing was unresolvable;
they were merely early. On a moving market the next tick replaces what was lost,
which is why this never showed as a fault; on a quiet one, and on every
rarely-traded contract, the loss is permanent.

Fixed by `runtime/pending_instrument_updates.py`: the newest unresolved update
per instrument is held, bounded by `unresolved_broker_update_hold_limit` (2000 =
Upstox's own documented `ltpc_combined_limit`), and released on the tick its
listing lands. Newest-per-key rather than a queue, because all three of these
are levels — an LTP restates the last print, a depth update carries full depth
rather than deltas, and Upstox restates the forming bar continuously.

**`market-data` could not carry an Indian index price at all** — the crypto
shape this feature was still holding. `broker-market-data-bridge` dropped any
LTP update whose `last_traded_quantity` was absent, on the reasoning that a
consumer needs a real size and a fabricated 0 would lie. Measured against the
live tape of **Friday 2026-09-04, a real trading day**, 1,500 instruments
sampled:

    75.1% of LTP updates carry no last_traded_quantity
      NSE_COM  100%     NCD_FO 100%     BCD_FO 100%     NSE_INDEX 100%
      BSE_FO    78%     BSE_EQ  59%     NSE_FO  51%     NSE_EQ     50%

A crypto trade stream is a stream of prints and every print has a size. Upstox's
LTPC is a *price* ticker that states a size only when it has one, and an index
has no traded quantity at all — so the rule kept the price of NIFTY off the wire
permanently. `NormalisedTrade.quantity` is now `float | None` and
`quote_volume` with it, exactly as `side` already was, and the four parts that
genuinely read size skip an unsized print rather than counting it as zero:
`order-flow-state-encoder`, `market-anomaly-detector`, `symbol-profile-store`
(spread and move still observed; only the volume estimator sits it out, so
`TYPICAL_VOLUME` reads absent rather than zero) and `liquidity-grader`.

### Verified after the fix — spine restarted 2026-09-06 06:29 UTC, market still closed

    broker-market-data-bridge   published market-data          4,137 in 30 s   (was 0 ever)
    broker-candle-bridge        published candle                   92 in 30 s   (was 0 ever)
    broker-order-book-bridge    published order-book-snapshot   2,828 in 30 s   (was 0 ever)

and the chain behind them is carrying for the first time:

    price-level-sampler   market-data 205 in    symbol-price-frame 930 out
    tick-size-resolver    order-book  206 in    price-increment  1,955 out
    liquidity-grader      both        405 in    liquidity-grade    609 out
    kline-window-builder  candle       24 in    kline-window       168 out
    market-anomaly-detector                     market-anomaly     651 out
    paper-fill-simulator  market-data 201 in
    execution-cost-model  market-data 195 in    cost-estimate   73,136 out

The feature's own reading went from **202 carrying / 83 idle** to **273 carrying
/ 12 idle**, and every running part in it now has all of its declared inputs
carrying except the two named below. The three bridges' standing also shows the
hold doing its job rather than merely existing: 1,604 updates awaiting a listing
396 into the 2,000-instrument conveyor cycle, 396 released, 0 dropped at the
limit.

**A fourth crash, found only by restarting.** `execution-cost-model` crash-looped
on `volume.get(key, 0.0) + trade.quote_volume` the moment real unsized prints
reached it — a reader the first grep missed. `liquidation-cluster-mapper` and
`cross-venue-price-consolidator` had the same shape and were guarded before they
could do it too. This is the failure mode CLAUDE.md already names: a green test
suite does not prove the thing that is running can still start, and the running
spine held the old code in memory until it was restarted.

### Crypto-shaped parts in this feature

Ten of the 23 are the crypto venue layer and are **off, not converted**:
`ccxt-venue-reader`, `venue-trade-stream-reader`, `venue-quote-stream-reader`,
`order-book-reader`, `venue-pool-rotator`, `cross-venue-price-consolidator`,
`stream-budget-planner`, `symbol-catalogue-reader`, `ban-signal-detector`,
`api-key-pool-rotator`.

Five of them already have a working Indian equivalent running beside them — the
`broker-*-bridge` parts and `broker-market-feed-reader` — so the replacement
exists and the crypto original is simply not switched on. **The other five have
no Indian equivalent yet and are the open question this walk leaves:**

| off crypto part | what the Indian equivalent would have to be |
|---|---|
| `stream-budget-planner` | who decides which 2,000 of 102,940 instruments the one Upstox connection subscribes to. `broker-market-feed-reader` currently decides it itself: universe first, then the master in catalogue order up to the cap. |
| `api-key-pool-rotator` | six brokers, six token lifecycles. Nothing rotates them today; `broker-token-standing` exists and one broker is connected. |
| `venue-pool-rotator` | the same question at the broker level — goal.md item 5's "the rest absorb API load" has no part that spreads it. |
| `ban-signal-detector` | an Upstox rate-limit or throttle response is a real thing and nothing watches for one. |
| `symbol-catalogue-reader` | superseded by `broker-instrument-catalogue-reader`; confirm and retire rather than leave declared. |

### What this feature is measurably missing

- **A subscription that matches what the bots trade.** `broker-market-feed-reader`
  reports `universe_instruments_known: 40` and subscribes 2,000. Today's tape
  shows where the other 1,960 went: 473 NSE_FO, **413 NSE_COM**, 142 NCD_FO,
  128 NSE_EQ, 1 NSE_INDEX. More than a third of the connection's capacity is
  spent on commodity and currency derivatives that no segment bot trades, while
  cash equity — whose shortlist ranks a 2,444-name universe — gets 128 slots.
  This is the first thing to check on Monday's open and is the strongest
  candidate for a genuinely new part.
- **Nothing measures feed latency against the exchange's own timestamp.**
  `news-latency-meter` exists for news; there is no equivalent for prices,
  and Upstox states `last_traded_time_ms` on every update.
- **A missing part, and a genuinely necessary one: `broker-quote-bridge`.**
  `market-quote` has exactly one producer in the whole blueprint —
  `venue-quote-stream-reader`, which is crypto and off — so
  `quote-level-sampler` has never received anything, `symbol-quote-frame` has
  never been produced, and both of its readers are cut off:
  `spread-reversion-detector` and, more importantly for Monday,
  `instrument-selector`. A quote is that part's **fallback when the last trade
  is too old to size against**, and `NormalisedQuote`'s own docstring records
  why it was built: on the live run of 2026-08-24 `instrument-selector` refused
  525 of 9,945 intents for a price too old. An Indian option contract that has
  not printed for minutes while carrying a live bid and ask is the ordinary
  case, not the corner, so this is the difference between selecting a contract
  and refusing it. The data is already arriving — `BrokerOrderBookUpdate.levels`
  carries Upstox's own bid/ask prices and sizes, the same input
  `broker-order-book-bridge` already reads — so this is a bridge exactly
  parallel to the three that exist, not a new feed.

  **Built and verified the same day.** Blueprint edit
  `dashboard/blueprint_edits/apply_2026-09-06_broker_quote_bridge.py` (proposal
  `docs/proposals/broker-quote-bridge.md`), part
  `parts/market_data_feed/broker_quote_bridge.py`, on the live spine. Measured
  30 s after a restart, market closed:

      broker-quote-bridge   market-quote        106 published   551 instruments resolved
      quote-level-sampler   market-quote        106 received    symbol-quote-frame 92 published
      instrument-selector   symbol-quote-frame   46 received    symbols_with_a_quote 244
      spread-reversion-detector                  47 received

  `symbol-quote-frame` had never been produced by anything, ever, and
  `instrument-selector` now holds a live quote for 244 symbols where it held
  none. `quotes_refused_for_a_one_sided_book` reads 307 over the same window,
  which is the guard doing real work rather than sitting unexercised: thin
  option books quoted on one side only are common, and a mid built from one is
  half the bid. The feature is 24 parts now, not 23.

### Open, not answered by this walk

Whether the fixes actually put messages on those three wires can only be
answered against a moving market. The counters were taken on a Sunday, six
minutes after a spine restart, with the feed delivering one snapshot and then
going quiet (`decoded_messages: 4`). **Monday 2026-09-07, 03:45 UTC** is the
measurement; `ajit-open-watch.timer` is armed for 03:40 UTC and
`operate/watch_market_open.py` samples the chain across the open.

---

## 2. `broker-adapter` — walked 2026-09-06 (market closed)

9 parts, all 9 running, all 9 launchable. Started at **151 wires carrying, 27
idle**; ended at **177 carrying, 1 idle**, with every part's every declared
input and output carrying. Three defects, and the first one was the largest
single finding of the audit so far.

### Every Upstox REST call in the project was blocked, and nothing said so

`broker-margin-quoter` read `calls_made 1,016 / calls_failed 1,016 / quotes_read 0`
and `broker-account-funds-reader` read `reads 34 / failures 34`. A 100% failure
rate on both, for the life of the process.

The cause, reproduced against the live API with the same token and payload:

    HTTP 403  Error 1010: browser_signature_banned
    "The site owner has blocked access based on your browser's signature."
    "**Do not retry.** Your user-agent has been banned by the site owner."

Upstox sits behind Cloudflare, and Cloudflare bans the stdlib's default
`Python-urllib/3.14` User-Agent. Not an auth failure, not token-dependent, and
by Cloudflare's own instruction it never recovers on its own.

**This mattered most for the goal that is due first.** `broker-margin-quoter` is
the only producer of `broker-margin-requirement`, and `leverage-selector` sizes
the 5x intraday equity bot against it — the machinery `docs/goal.md` records as
"already resolved" in commit `b0a2d37`. It was resolved in the code and dead on
the wire: the leverage path had never once received a real broker margin.

**Why it stayed invisible is its own finding.** The part does hold the reason —
`last_failure` is on `describe_quoting`'s output — but `countable_standing`
(`runtime/part_process.py`) keeps numeric values only, so the one field that
explained a thousand failures was dropped before it reached any board. Every
visible counter looked like a part doing its job. Nothing in this project would
have surfaced it except asking, per part and per wire, whether anything had
actually travelled — which is what this audit is.

**The knowledge already existed and had not travelled.** `broker-history-reader`
hit this exact Cloudflare block on 2026-09-02 and answered it with `curl_cffi`'s
`impersonate="chrome"`. That worked, and it was applied to exactly one of the
four HTTP call sites in the project; the other three were never told.

Fixed by `runtime/brokers/broker_http_request.py` — one builder every broker
request goes through, carrying a User-Agent that **names this client** rather
than impersonating a browser. Verified the same day against the live API:

    no User-Agent    margin -> 403 (cf 1010)    funds -> 403 (cf 1010)
    this User-Agent  margin -> 200              funds -> 200

No browser impersonation is needed anywhere, which also means
`broker-history-reader`'s chrome impersonation is heavier than the problem
requires (verified: the plain header returns 200 on that endpoint too). Left as
it is for now — it works, and changing a working call path is not this walk's
job — but recorded so it is a decision rather than an oversight.

### History was fetched over windows that contain no trading day

`broker-history-reader` read `requests_planned 233 / windows_already_read 32 /
candles_published 0` — 32 windows fetched, never a single bar published, ever.

`broker_history_most_days_back` was **1**, and it counts **calendar** days. The
window is `(today - most_days_back) .. today`, and this part deliberately fetches
only while the market is shut, so on a Saturday the window is Friday..Saturday,
on a Sunday it is Saturday..Sunday, and on a Monday morning before the bell it is
Sunday..Monday. Measured against the live API on Sunday 2026-09-06, same
instrument and interval:

    most_days_back = 1    0 candles
    most_days_back = 7    1,875 candles, back to 2026-08-31 09:15 IST (4 sessions)

Raised to 7. Seven calendar days always spans at least one NSE session and stays
far inside Upstox's one-month-per-request bound for 1-15 minute intervals. This
is what primes `kline-window-builder` **before** an open rather than after it,
which is the entire reason history is replayed while the market is shut — and it
matters on Monday, because after a restart the arbiter stands aside until
conviction is back.

Verified after the fix: `candles_published 7,187`, `prints_published 7,187`.

### Crypto-shaped parts in this feature

None. This is the Indian-markets feature and every part in it is already
Indian-shaped. The 9 wires reading `NOT MEASURED` all run to crypto parts that
are off elsewhere in the blueprint, not to anything inside this feature.

### What this feature is measurably missing

- **A fault when a broker call fails, not just a counter.** 1,016 consecutive
  failures produced no fault, no red tile and no journal line — only a number
  nobody was reading. `countable_standing` dropping non-numeric standing is
  correct for what it does, but it means a part's *reason* for failing can never
  reach a board (Rule 8: every status carries its proof). The narrow fix would
  be a numeric `seconds_since_last_success` or a `part-fault` on a run of
  failures; naming it here rather than building it, because it is a
  runtime-substrate change and belongs in its own piece of work.
- **Only one of the six brokers exists.** `docs/goal.md` item 5 wants Upstox
  primary with the other five covering data redundancy and API-load spreading.
  The adapter contract is there and Upstox is the only implementation, so there
  is no fallback if its feed or API goes down mid-session. That is planned work
  rather than a defect, but this feature is where it lands.
