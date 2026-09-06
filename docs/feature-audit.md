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
| features walked | **5 of 29** |
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
| 3 | `execution-venue-adapter` | **walked 2026-09-06** — 9 of 11 are the crypto real-money path, correctly off for paper trading; no Indian equivalent exists yet |
| 4 | `paper-live-trading` | **walked 2026-09-06** — chain is idle-because-no-trade, not broken; `implied-vol-reader` converted off crypto |
| 5 | `opportunity-scanner` | **walked 2026-09-06** — a fourth checker built (declared inputs nothing reads); the learning chain proved end to end on a real replayed trade |
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

---

## 3. `execution-venue-adapter` — walked 2026-09-06 (market closed)

11 parts, 2 running. 27 wires carrying, 10 idle, 176 not measured.

**Nine of the eleven are the crypto real-money execution path** —
`ccxt-order-router`, `order-state-poller`, `order-resubmitter`,
`order-reject-classifier`, `order-not-found-debouncer`,
`venue-order-status-translator`, `venue-balance-reader`,
`venue-position-reader`, `venue-rate-budgeter`. All off, and **correctly off**:
in paper mode the path is `order-destination-router → order-request →
paper-fill-simulator → fill`, which touches none of them.

So this feature is not broken; it is a real-money path with **no Indian
equivalent built at all**. `broker-adapter` has a funds reader, a history
reader, a catalogue reader, a margin quoter and a feed reader, and no order
router, no order-status poller and no fill translator. That is correct
sequencing — `docs/goal.md` item 7 is paper before live, and SEBI's Feb 2025
algo-ID and static-IP requirement blocks live API order placement regardless —
but it means **Phase A's "complete end-to-end" is not complete on the execution
side**, and the work has not started. Named here so it is a known gap rather
than a surprise.

The two running parts (`limit-price-walker`, `resting-order-cancel-policy`) show
`order-request` not carrying, which is `NOTHING YET`: no order has been placed
on this live run.

## 4. `paper-live-trading` — walked 2026-09-06 (market closed)

10 parts, 9 running (`paper-liquidation-simulator` is deliberately off — an
intraday equity position is squared off by the broker, not liquidated at a
price). 126 wires carrying, 49 idle, 49 not measured.

Every idle wire in the order chain is idle for one reason: **no order has been
placed on this live run**, so `bounded-order`, `stamped-order`, `order-request`,
`delayed-order-request` and `fill` have nothing on them. That is `NOTHING YET`
and not a fault — the same chain opened and closed trades on the captured tape
on 2026-09-04.

### Tracing why nothing has been raised, which is the Monday question

Walking the funnel backwards, **not one of the seven `entry-candidate`
producers has published a single candidate** on this run. The five detectors are
all receiving and evaluating, and their refusals are honest and market-shaped:

    mean-reversion-detector    121,114 observations, symbols_with_a_full_window 0, deepest_window 3
    momentum-burst-detector    120,716 observations, 398 symbols tracked, not a burst
    volatility-gap-detector    2,672 tests, no_implied_surface 2,057
    expiry-day-zero-to-hero    instruments_expiring_today 0  (it is Sunday)

A frozen weekend snapshot has three distinct prices in it, so an empty window is
the correct answer, not a defect. Monday's open is what tests this.

**One branch is dead independently of market hours.**
`universal-symbol-sweeper` has run 3,202 sweeps and raised nothing, and it never
can as things stand: it tests `watch-condition`s, and it has received **zero**.
`watch-condition-compiler` reports `compiled 0 / active_conditions 0` and has
received nothing itself, because `instruction-writer` reports `requests 0 /
written 0`. The whole autonomous instruction-generation loop has never produced
an instruction, so the sweeper is a detector with nothing to detect. Not
Monday-blocking (the five detectors are the primary path) but it is a
permanently dark branch, and it is the honest answer to "why does the universal
scanner never fire".

### `implied-vol-reader` converted off crypto

`no_implied_surface: 2,057` against `tests: 2,672` had one cause:
`implied-vol-reader` had `reads 0 / quotes_seen 0 / surfaces_published 0` for
its entire life. Its `start_part` drained `market-data`, threw it away and
returned no read requests — a deliberate stub whose own docstring cited RL-050,
**the crypto build order**, under which options were a segment this system had
not built. That ordering is retired: Phase A is both options segments, first.

Converted rather than replaced (goal item 3): the reader's core knows nothing
about a venue and every rule in it is what an Indian chain needs. Only its input
was crypto-era. It now reads `broker-option-greeks` (the broker's own implied
volatility — read, never solved locally), `broker-subscribed-instrument-listing`
(strike, expiry, CE/PE), `market-quote` (the two-sided market, from
`broker-quote-bridge` built the same day) and `symbol-price-frame` (the
underlying's spot). All four were already carrying; nothing new is fetched.

Two defects the conversion exposed and fixed:

- **An append-only quote list.** `observe_quote` appended per underlying and was
  cleared only by `release()`. Harmless while the part received nothing; on a
  live chain at hundreds of quotes a second it grows without bound, and the
  staleness filter does not help because it runs at read time and leaves what it
  dropped in the list. Now the newest quote per contract — a quote is a level.
- **`underlying_key` cannot name an underlying.** Resolving it needs the
  underlying's *own* listing, and measured on the real subscription only **26 of
  1,707** subscribed options had their underlying subscribed too — a reader
  waiting for it would wait for ever on 98% of the chain. Upstox's master states
  `underlying_symbol` on every one of its 94,352 options (17,800 of which carry
  no `underlying_key` at all), and the adapter was parsing the key and throwing
  the name away. Now carried on `InstrumentListing`. It equals the underlying's
  own trading symbol for every NSE_FO (32,008) and BSE_FO (4,170) option — index
  and stock options, exactly Phase A — and deliberately not for a commodity
  option, whose underlying is a futures contract carrying an expiry in its name.

Verified live after the rewire: **`quotes_seen 8,037`**, from 0 for the part's
whole life, with all four inputs carrying and `options_feed_connected` reading
true from the data rather than from configuration.

`reads` is still 0, and the reason is the finding below rather than the
conversion.

### The finding that outranks everything else before Monday

`read` needs the underlying's spot, and **the feed does not subscribe the
underlyings of the options it subscribes**. Measured on the real subscription of
2026-09-06 — 1,915 instruments the feed actually delivered, 1,707 of them option
contracts:

    underlying     contracts   its own price subscribed
    SILVERM              511   no
    MIDCPNIFTY           205   no
    USDINR               176   no
    GOLD                 121   no
    JPYINR                67   no
    OFSS                  62   no
    ...
    options whose underlying is also subscribed:  26 of 1,707

The top five underlyings by contract count are **silver, a mid-cap index,
two currency pairs and gold** — not one of them a thing any of the three segment
bots trades. NIFTY, the index-options bot's own chain, is not in the top ten.

This is the same root cause first named walking `market-data-feed`
(473 NSE_FO / 413 NSE_COM / 142 NCD_FO / 128 NSE_EQ on the tape) now measured
precisely: `broker-market-feed-reader` fills its 2,000-instrument connection
with *the universe first, then the master in catalogue order up to the cap*, and
catalogue order is essentially instrument-key order, which is why commodity and
currency derivatives dominate. Every downstream consequence follows from it —
no vol surface, no spot for a quoted chain, and an option chain the bots do not
trade.

### Fixed the same session — the subscription now selects instead of reordering

`broker-market-feed-reader` had a prioritiser (`prioritize_index_option_chain`,
added 2026-09-02 for exactly this) and it was not enough, for two compounding
reasons:

1. **It filtered `instrument_type == "INDEX"`.** It was written when index
   options were the only segment. `underlyings_every_built_segment_trades`
   returns the union across every built segment — 3 indices for index-options
   and **14 ordinary shares** shared by stock-options and cash-equity-intraday —
   and the INDEX filter silently dropped all 14. The stock-options bot's chains
   were never prioritised at all, and neither were the spot prices cash equity
   needs. Measured against the real master: INDEX-only selects 3 underlyings and
   723 nearest-expiry contracts; every tracked type selects **15 and 1,528**.
2. **It only reordered.** It handed the whole catalogue to `plan_subscriptions`,
   which takes whatever fits the cap in listing order — so every slot the
   priority set did not claim went to whatever was next in raw catalogue order.
   And **a slot spent on the wrong instrument is spent for good**: the
   connection caps at 2,000 and nothing evicts, so once the catalogue race
   filled it there was no room for the chain the bots were waiting on, however
   long the connection stayed open.

It is now `only_what_the_segments_trade` — a filter over every tracked
underlying whatever its instrument type, plus their nearest-expiry chains, and
nothing else. The rest of the connection stays empty on purpose: an empty slot
costs nothing and can still be filled, a wrongly-spent one cannot be recovered.
The same name listed on two exchanges keeps both chains (a symbol-keyed dict was
dropping one).

**Measured on the live spine, before and after:**

    before   1,915 delivered, 1,707 options
             SILVERM 511, MIDCPNIFTY 205, USDINR 176, GOLD 121, JPYINR 67
             options whose underlying is also subscribed:   26 of 1,707
             NIFTY not in the top ten

    after      609 delivered,   588 options   (still filling as the catalogue streams)
             NIFTY 184, MARUTI 96, RELIANCE 78, HINDUNILVR 74, SBIN 72,
             TCS 38, TATASTEEL 20, KOTAKBANK 20, INFY 6
             options whose underlying is also subscribed:  588 of 588

Not one commodity or currency contract, and every single option's underlying is
priced. Downstream, on a closed market:

    implied-vol-reader     reads 263, quotes_seen 1,175, surfaces_published 90   (0 ever before)
    instrument-selector    implied_vol_surfaces_seen 263                          (0 ever before)

`reads_too_thin_for_a_surface: 173` of 263 is the honest weekend answer — few
contracts carry a two-sided quote when the market is shut — and it is the state
the reader is built to report rather than smooth over.

---

## 5. `opportunity-scanner` — walked 2026-09-06 (market closed)

11 parts, 10 running, 10 launchable. 153 wires carrying, 59 idle, 26 not
measured. `news-catalyst-detector` is the one part here with no `start_part` at
all — it consumes `news-item` and `news-impact-forecast` from
`stock-market-news-data`, 24 of whose 29 parts have no code, so it is blocked on
a whole unbuilt feature rather than on anything in this one.

### A fourth class of defect, and the checker that ends it

`expiry-day-zero-to-hero-detector` declared `broker-price-frame` and the type
appeared **exactly once in its entire source** — in the `PART_DECLARATION`
consumes tuple — and nowhere else. `broker-price-level-sampler` was publishing
5,868 price frames at the time, so the audit board showed a wire whose producer
was healthy and whose consumer received nothing: indistinguishable from a real
delivery fault until somebody opens the file.

None of the three existing checkers can see this. `check_contracts.py` checks
the blueprint against itself and never opens a part's source; the other two
check the code against the declaration for types the code *does* read. Nothing
checked the declaration against the code for a type the code reads **not at
all**. That costs more than a stray tuple entry: R-01 computes every edge from
consumes/produces, so a declared-and-unread input is a wire on every diagram, a
row in the wiring explorer, and a permanent `NOT CARRYING` line blamed on a
working producer. RL-067 in the direction nothing was enforcing.

A scan of all 348 launchable parts found **six**. Five resolved by asking what
each part actually needs:

| part | dropped | because |
|---|---|---|
| `expiry-day-zero-to-hero-detector` | `broker-price-frame` | judges moneyness from Upstox's own delta, never from spot |
| `bull-feature-builder` | `symbol-universe` | one vector per candidate; the universe is what the scanner sweeps |
| `bear-feature-builder` | `symbol-universe` | same |
| `venue-trade-stream-reader` | `venue-standing` | crypto part being retired, and its producer is off too |
| `venue-quote-stream-reader` | `venue-standing` | same |

`dashboard/check_declared_inputs.py` stops the class recurring. **14 parts bind
readers dynamically** (`context.bus.reader(data_type)` in a loop over their own
consumed types) and are reported as *not statically checkable* rather than as
clean — counting those as passing would be the green-that-means-nothing this
project keeps finding.

### The sixth, and where it led

`opinion-arbiter` declares `bot-maturity` and binds no reader. Not a stray line:
how proven a bot is, is exactly what an arbiter should weigh. The operator chose
to **fix the source rather than the declaration**, and the source turned out not
to be broken.

`edge-graduation-gate` had `recv {}` on all seven inputs and `judgements 0`, and
all seven of its producers were running and publishing nothing. Tracing them,
every one waits on `closed-trade` — and **no trade has ever closed on a live
run**. `trade-cluster-detector` is the proof it is not a wiring fault: it was
receiving 119,888 correlation-clusters and still published nothing, because its
other input is `closed-trade`.

**The replay never tested any of it.** `operate/replay_a_captured_session.py`
imports seven parts — the fill simulator, stop manager, cost-basis tracker, fill
reconciler, excursion tracker, close detector, exit chainer — and mentions the
learning chain **zero** times. The 2026-09-04 replay closed 252 trades and not
one reached `pnl-attributor`. So the replay proved the *trading* half and the
*learning* half had never run on a real trade at all, which is worth saying
plainly because `docs/goal.md` records that replay as the evidence the three
segments work.

### The learning chain, driven with a real replayed trade

Rather than assume, the chain was driven end to end with a real closed trade
from that replay (`tests/integration/test_a_closed_trade_becomes_a_bot_maturity.py`):

    closed-trade -> pnl-attributor        pnl-attribution        usable
                 -> entry-quality-scorer  entry-quality          usable
                 -> luck-skill-separator  outcome-significance   usable
                 -> trade-episode-encoder trade-episode          encoded
                 -> bot-scorekeeper       bot-scorecard          1 trade recorded
                 -> edge-graduation-gate  bot-maturity           graduated, may_trade_live

**The subsystem works. It has simply never been fed.** That is the answer to
"why has `bot-maturity` never been produced", and it means `opinion-arbiter`'s
declaration is a live intent waiting on the first closed trade rather than a
dead line — so it stays, and the new checker stays an audit instrument rather
than a commit gate until it does.

### Two defects that could only ever appear on the first closed trade

Found the first time the chain was driven, both now guarded in the parts:

- **`pnl-attributor` published a refused attribution.** When it cannot attribute
  (`no-fill-was-recorded-for-this-trade`) it still built a `PnlAttribution` with
  every component at `0.0`, `unexplained` holding the **whole** realised PnL,
  and **`reconciles=True`** — because unexplained absorbs everything, so the
  reconciliation trivially holds. A consumer cannot tell that from a measured
  attribution: it states a PnL, it says it reconciles, and its residual is a
  number. `trade-episode-encoder` reads `cost_share` and `residual` off it and
  its own comment is *"only measured values go into conditions: this is what a
  model reads"*. Its refusal-on-incomplete would have been defeated by a
  complete-looking fabrication.
- **`luck-skill-separator` published a refused significance**, carrying
  `standardised=None` and `is_measurable=False`, straight into those same model
  conditions.

`entry-quality-scorer` already had the `if scored.is_usable` guard; the other
two now match it. Withheld, the encoder correctly reports `INCOMPLETE` and names
what has not landed, which is what it was built to do.

### What this feature is measurably missing

- ~~**The replay does not exercise the learning half.**~~ **Done 2026-09-06**,
  at the operator's instruction — see below.
- **`stop-placement-audit` has no producer at all** — it is an optional piece of
  a trade episode that nothing in the blueprint writes.
- The instruction loop remains dark: `instruction-writer` has had 0 requests, so
  `watch-condition-compiler` compiles nothing and `universal-symbol-sweeper` has
  swept 3,202 times against zero conditions.

---

## The replay runs the learning half — 2026-09-06

`operate/replay_a_captured_session.py` imported seven parts and stopped at
`position-close-detector`. A closed trade now walks on through
`pnl-attributor` → `entry-quality-scorer` → `luck-skill-separator` →
`trade-episode-encoder` → `bot-scorekeeper`.

**Measured on the captured tape of 2026-09-04**, 10 closed trades across all
three segments:

    10 closed trade(s) decoded, 10 became a trade-episode
    attribution    10 attributed
    entry quality  10 scored
    significance   10 unlikely-to-be-noise
    bot-scorecard  bull: 10 trade(s), 3 win(s)
    episodes refused for a missing piece: none

What is real and what is not, on the same terms the script already set:

- **Real** — the fills are the replay's own, priced by Upstox's real charge
  stack; the attribution is computed across them; the entry-quality window is
  the captured prints in the `entry_quality_window` seconds after the entry,
  which is what the scorer means by "the window the decision could have acted
  in"; and the significance is each trade's return over that contract's **own**
  measured daily volatility, computed from its own print-to-print returns.
- **Stated** — the detector, the regime and the opinion's probability, because
  the replay opens at the first print by construction, so no detector fired and
  no bot stated a conviction. Named `replay-opened-at-the-first-print` and
  `unclassified-in-replay` in the output so nothing reads as measured that was
  not. What *is* real on the scorecard is the half that matters: whether each
  trade made money, and how much.
- **Not run** — `edge-graduation-gate`. It needs a decision quality, a
  refutation verdict, a trial verdict and a coverage report, and a replay
  produces none of the four. Driving it would mean inventing all four and
  calling the result a graduation, which is the fabrication the encoder's own
  refusal exists to prevent. What it would need is printed instead.

### The defect the trading half could never have shown

`paper-fill-simulator` stamps a fill `filled_at_ns=self._now_ns()` and
`position-close-detector` stamps `closed_at_ns=self._now_ns()`. Live that is
exactly right. In a replay it was **the wall clock of the machine running the
replay**, so a round trip that took forty minutes of market time was recorded as
having been held for the few milliseconds the replay took to walk its prints.

Net PnL does not depend on the clock, which is why nothing ever noticed. The
learning half does: `luck-skill-separator` scales a symbol's volatility to the
horizon actually held, so a millisecond hold made the expected noise vanish and
every trade read as thousands of standard deviations from it.

    before   -1958.31   -1779.31   2501.50   2948.04   774.53   -3069.10   -5689.92
    after       -6.90      -2.80      4.00      3.87     1.96      -6.40      -9.24

    holding periods, after: 36s, 194s, 205s, 181s, 1,494s, 387s, 243s, 3,596s

`TapeClock` hands those two parts the captured session's own time, advancing to
each print as it is walked, and never backwards — a clock that went back would
make a holding period negative, which reads as a trade that closed before it
opened.

**This was a replay-only defect and not a live one** — `now_ns()` is the correct
stamp on the live spine. It is recorded here because it is the exact shape this
audit keeps finding: a number that looks measured, that nothing downstream could
tell from a real one, in a path nobody had ever run.
