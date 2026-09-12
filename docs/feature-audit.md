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
| features walked | **29 of 29** |
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
| 6 | `segment-bot` | **walked 2026-09-06** — one part, 9 of 11 inputs carrying; idle downstream of an intent nothing has raised |
| 7 | `risk-capital-allocation` | **walked 2026-09-06** — 16 of 17 running, every idle wire downstream of `trade-intent` |
| 8 | `portfolio-state` | **walked 2026-09-06** — idle downstream of `fill`; found the account settling in USDT while charging rupees |
| 9 | `capital-desk` | **walked 2026-09-06** — 8 of 8 running, 125 wires carrying, 8 idle |
| 10 | `bull-bot` | **walked 2026-09-06** — 10 of 10 running, starved at `entry-candidate`; identical to bear |
| 11 | `bear-bot` | **walked 2026-09-06** — identical shape to bull, 129 carrying / 75 idle |
| 12 | `profit-tailgating-bot` | **walked 2026-09-06** — same funnel; peers stay isolated (R-03 holds) |
| 13 | `prediction` | **walked 2026-09-06** — 14/15 running, 216 carrying, only **5 idle**; `implied-vol-reader` now 4/4 in, 2/2 out |
| 14 | `ledger` | **walked 2026-09-06** — 5 of 6 running, every idle wire downstream of a trade; the off part is crypto funding |
| 15 | `observability` | **walked 2026-09-06** — 755 wires carrying, the most of any feature; `clock-skew-monitor` converted off crypto and switched back on |
| 16 | `resource-governor` | **walked 2026-09-06** — 14/14 running, 491 carrying; nothing being shed, which is why `switch-record` is quiet |
| 17 | `closed-trade-decoding` | **walked 2026-09-06** — 20/20 running, no gaps; idle downstream of a closed trade |
| 18 | `learning-loop` | **walked 2026-09-06** — 18/18 running, no gaps; same |
| 19 | `intelligence` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 20 | `knowledge` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 21 | `hypothesis` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 22 | `ai-brain` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 23 | `skills` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 24 | `backtesting` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 25 | `llm-foundation` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 26 | `llm-services` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 27 | `online-research` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 28 | `autonomous` | **walked 2026-09-06** — all parts running, no gaps; idle downstream of a trade or an LLM call |
| 29 | `stock-market-news-data` | **walked 2026-09-06** — re-walked 2026-09-12: 10 of 29 built and running; the source and the four parts directly below it landed that day, see the end of this file |

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

---

## 6 and 7 — `segment-bot` and `risk-capital-allocation`, walked 2026-09-06

`segment-bot` is one part, `instrument-selector`: 9 of 11 inputs carrying, 21
wires carrying. `risk-capital-allocation` is 16 of 17 running, 231 carrying, 69
idle. The single part off is `margin-liquidation-watch`, which is the crypto
liquidation watcher and correctly off (an intraday equity position is squared
off by the broker, not liquidated at a price).

**Every idle wire in both is downstream of `trade-intent`, and nothing has
raised one.** `opinion-arbiter` reports `intents_formed: 0` and receives
`competence-map` and `market-regime` but no `directional-opinion` — which is
what it arbitrates. That traces back through conviction, feature vectors and
candidates to the same dry funnel feature 4 recorded: correct on a closed
market, and Monday is what tests it. The intent-to-fill path itself is not
untested — `tests/integration/test_an_intent_becomes_a_paper_fill.py` covers it
and passes.

## The scan that replaced walking one feature at a time

Walking a feature answers "what is this one doing". It does not answer "where is
something actually wrong", because on a shut market most wires are honestly
idle. So the audit instrument now reports the one signature that is never
honest:

    a wire whose producer has published, whose consumer has received none,
    with both ends running

`python3 dashboard/audit_feature_dataflow.py` prints it under every summary.
Across all 29 features and 10,997 wire readings it found **three**, all on one
part.

### `instruction-replayer` was dropping tens of thousands of messages

It binds four readers and drained them only inside a replay. A replay needs a
`walk-forward-split`, none had ever arrived (`runs: 0`), so three of those
inboxes were never drained at all — and an undrained inbox fills, after which
every datagram sent to it is refused:

    execution-cost-model       52,989 published   12,606 NOT delivered
    fill-volume-capper            513 published   31,503 NOT delivered
    intra-bar-fill-sequencer      513 published   31,503 NOT delivered

`fill-volume-capper` had **61 refusals for every message that landed**. Nothing
reported a fault: the producer is entitled to publish and the consumer is
entitled to be busy, and the only trace was a counter nobody was reading.

This is a shape the project has met before — `subscribed-instrument-listing-filter`
carries its own note that "every drop is an inbox that was full when a datagram
arrived", and the 2026-08-26 storm was the same thing at scale. The fix is the
one that note already prescribes: **drain every tick, and keep the expensive
`mapping()` where it was.** Draining is cheap; copying the table is not.

Verified after the restart:

    instruction-replayer   cost-estimate 188, fill-sequence 3,462, fillable-size 3,415   (all 0 before)
    execution-cost-model        950 published, 0 not delivered      (was 12,606 dropped)
    intra-bar-fill-sequencer  3,462 published, 0 not delivered      (was 31,503)
    fill-volume-capper        3,415 published, 47 not delivered      (was 31,503)

---

## 8 and 9 — `portfolio-state` and `capital-desk`, walked 2026-09-06

`capital-desk` is the healthiest feature walked so far: 8 of 8 running, 125
wires carrying, 8 idle. `portfolio-state` is 6 of 7 running with 63 idle wires,
every one of them downstream of `fill` — no trade has been made. Its one off
part is `liquidation-price-tracker`, correctly off for the reason already
recorded (an intraday equity position is squared off by the broker, not
liquidated at a price).

### The account was settling in USDT while charging rupees

`usdt-pnl-accountant` computes every trade's profit and loss, and
`settlement_currency` — the operator setting it and four other parts read — said
**USDT**, with a note about "the currency both venues settle the traded
perpetuals in". A crypto-era value that outlived the venues it named.

Meanwhile every fee this project charges is Upstox's real rupee stack
(`options_stt_sell_rate`, `equity_intraday_brokerage_rate` and the rest), every
price on the tape is in rupees, and every paper account starts at 500,000
rupees. **The statements were rupees wearing a USDT label** — not a wrong
number, a number with the wrong unit on it, which is the kind of thing that only
becomes wrong when somebody acts on it.

### Changing the setting alone would have crashed the attributor

`pnl-attributor` read `settlement_currency` from settings for the fills it
recorded, and compared against a **hardcoded** `QUOTE_CURRENCY = "USDT"` in
`_to_usdt` and `_attribution`. So the moment the setting stopped saying USDT, a
fill recorded in the real currency would have failed that equality, gone looking
for `self._conversion_rates["INR"]`, and raised — on the first closed trade.

Two things that read the same fact from two places, free to disagree, and the
disagreement was latent until somebody changed the one that was configurable.
The part now takes the currency through its constructor from that setting, which
is what made the setting safe to change. `_to_usdt` is
`_in_settlement_currency`, and `observe_conversion_rate`'s `rate_to_usdt` is
`rate_to_settlement`.

`settlement_currency` is now **INR**. Verified: 4,282 tests pass, the spine
restarts clean, and no currency or KeyError appears in the journal.

### Still crypto-shaped here, and named rather than fixed

The *names* remain: `usdt-pnl-accountant`, `usdt-pnl-statement`,
`UsdtPnlStatement`. Renaming a part id and a data type touches the blueprint,
the part, four consumers (`board-snapshot-builder`, `reward-shaper`,
`allocation-rebalance-proposer`, `decision-cost-accountant`), the boards and the
tests. That is a real piece of work and it is not a correctness fix — the unit
is right now, only the label is stale — so it is recorded here rather than
rushed the day before the first live session. 61 files still name USDT.

---

## 10-12 — the three peer bots, and the structural scan, 2026-09-06

Bull and bear are **identical**: 10 of 10 running, 129 wires carrying, 75 idle,
and both starved at the same place — `bull-setup-filter` and
`bear-setup-filter` receive no `entry-candidate`. Same funnel feature 5 traced.
R-03 holds: the peers carry nothing between each other.

Walking a feature at a time stopped paying here, because on a shut market most
wires are honestly idle. Two structural scans replaced it:

**Types consumed by nobody's producer: zero.** Every type someone reads has a
part that writes it.

**Types whose every producer is off: twelve**, and eleven are the crypto layer
being retired — `raw-venue-order-status`, `order-reject-reason`,
`venue-position-report`, `venue-rate-budget`, `key-standing` (real-money
execution), `liquidation-map`, `liquidation-price` (crypto liquidation),
`consolidated-price`, `stream-plan`, `venue-standing` (crypto venue layer),
`funding-settlement` (perpetual funding). `venue-standing` has **8 consumers**,
the largest dead type, and its Indian equivalent — noticing an Upstox throttle —
is the gap already named in feature 1.

The twelfth was not crypto, and led somewhere.

## The safety net was deaf, and the detector was measuring its own noise

`probe-runner` looked off. It was not: it reports every 0.3s. What was real was
the warden beside it, which had escalated the same part 1,717 times with
`refused_system_wide_cause` at **4,671**.

`failing-part-detector` had raised **15,109 faults, every single one the same
kind** — `taking-longer-every-tick`. The code itself said the cause was
unknown: *"Why this fires on ~3% of all checks is still unexplained, and it is
now the whole of the warden's remaining escalation volume."*

**The cause.** Both halves of the comparison come out of **one rolling window**:

    recent  = window[-half:]      earlier = window[:half]

so once the window is full, `earlier` is not a baseline — it is simply half a
window ago. A ratio between two means of a fluctuating series crosses any fixed
threshold at a rate set by its variance, for ever. It was measuring variance and
reporting it as a trend.

**What that cost.** `taking-longer-every-tick` was in the warden's
`RESTARTABLE_FAULTS`, so the warden restarted six healthy parts, and six is past
its system-wide ceiling — after which it refused every further fault as "one
cause rather than many independent faults". **A genuinely crashed part would not
have been restarted**, which is the single thing that part exists to do. The
safety net had been switched off by noise, and nothing said so.

Two changes, both measured on the live spine:

| | before | after |
|---|---|---|
| faults raised | 15,109 of 318,224 checks (4.75%) | 1,020 of 72,882 (1.4%) |
| warden restarts requested | 6 | 0 |
| warden refused system-wide | 4,671 | **0** |

1. **A slowdown must persist.** `detector_slowdown_checks` (5) requires the
   ratio to hold on consecutive checks — a fault named "taking-longer-*every*-
   tick" has to be more than one window crossing. The streak resets the moment
   it does not.
2. **A slowdown is escalated, not restarted.** Restarting is not a treatment for
   it — the fault's own remedy is "the tick time returning to its baseline,
   usually after whatever structure is growing is bounded" — and a part
   restarted because the machine is loaded adds load, which raises the same
   fault about its neighbours. The treatment caused the disease. It is still
   escalated (129 escalations), so nothing is hidden, and the ceiling stays for
   the crashes it was meant for.

---

## 14 and 15 — `ledger` and `observability`, walked 2026-09-06

`observability` carries **755 wires**, more than any other feature, and 9 of 10
parts run. `ledger` is 5 of 6, with every idle wire downstream of a trade that
has not happened — the recorders are waiting for `fill`, `closed-trade` and
`journal-entry`, which is correct.

Each has exactly one off part, and they are opposite cases.
`funding-settlement-recorder` is crypto perpetual funding: correctly off, no
Indian equivalent needed, an Indian option has no funding leg.
`clock-skew-monitor` was not.

### The part that would notice this machine's clock drifting had never run

It consumed exactly one type, `raw-venue-order-status`, whose only producers are
`ccxt-order-router` and `order-state-poller` — both crypto, both off. So it was
taken off the spine with that group, and the note explaining why is still in
`run_live_spine.py`.

Its own docstring says what that costs: *"the failure is quiet at first — an
occasional rejection that looks like bad luck — and then total: once drift passes
the window, every private order fails and the system is unable to trade while
appearing entirely healthy."*

**Half of it was never crypto.** `observe_venue_time(venue_id, venue_time_ns,
local_time_ns)` takes any stamped message and records the offset. Only the input
was crypto-shaped, and `broker-market-data` carries Upstox's own
`broker_time_ns` on every LTP update.

The motivation is not hypothetical. **Two timestamp traps were found by hand on
this one day**: Upstox's historical rows are `+05:30`, and NSE's intraday chart
writes IST wall-clock as though it were an epoch (3.88% disagreement against
this project's own tape read as UTC, 0.30% shifted back). Neither would have
been caught by anything running.

Rewired to read both — the rejection half is kept, not deleted, because a venue
saying "your timestamp is wrong" is the sharpest signal there is the day a real
order path exists — and switched back on. 324 parts reporting, up from 323.

**Not yet measuring, and the reason is the market.** `broker-market-data`
published **0 messages in 30 seconds** while this was checked: the feed sends
its snapshot at connect and then goes quiet on a shut exchange, and the monitor
starts after the feed so it missed that burst. Its standing reads
`venues_watched: 0`, which is the honest state rather than a healthy-looking
zero. Monday's first tick is what makes it measure.

---

## 13, 16, 17, 18 — the four that are simply working, 2026-09-06

| feature | parts | carrying | idle |
|---|---|---|---|
| `prediction` | 14 of 15 | 216 | **5** |
| `resource-governor` | 14 of 14 | 491 | 14 |
| `closed-trade-decoding` | 20 of 20 | 249 | 161 |
| `learning-loop` | 18 of 18 | 223 | 159 |

No unbuilt parts, no orphan types, no starved consumers. `prediction`'s one off
part is `liquidation-cluster-mapper` (crypto, correctly off), and
`implied-vol-reader` now reads **4/4 in and 2/2 out** — this morning's conversion
carrying in full. The governor's `switch-record` is quiet because nothing is
being shed, which is the correct reading of a machine that is not contended. The
two high-idle features are downstream of a closed trade, and that chain is
proven end to end by `test_a_closed_trade_becomes_a_bot_maturity.py`.

## 29 — `stock-market-news-data`: 5 built, 24 not

The five that run are the trading-mechanics half and they carry fully:
`corporate-action-reader`, `corporate-action-adjuster`,
`trading-restriction-reader`, `instrument-restriction-state`,
`market-session-calendar`. Feature-internal idle wires: **zero**.

The 24 that do not exist are one coherent subsystem — the news pipeline — and
they are the largest structural gap in the project. Mapped in dependency order
so this is a buildable plan rather than a gap:

**Layer 0 — the seven sources (each needs nothing unbuilt).**
`broker-news-reader`, `exchange-filing-reader`, `financial-press-feed-reader`,
`macro-event-calendar-reader`, `regulator-circular-reader`,
`results-calendar-reader`, `social-chatter-reader`. Every one is an external
integration; that is the real cost of this feature, not the parts above them.

**Layer 1 — eight that need one source.** `news-item-deduplicator`,
`news-latency-meter`, `news-source-health-monitor`, `news-text-structurer`,
`news-symbol-resolver`, `news-category-classifier`, `news-history-reader`,
`news-reaction-labeller`, `unexplained-move-investigator`.

**Layer 2 — six that need those.** `news-credibility-scorer`,
`news-novelty-scorer`, `news-sentiment-model`, `news-surprise-scorer`,
`news-tape-writer`, `web-news-searcher`.

**Layers 4 and 7 — the two terminal ones.** `news-segment-classifier`, then
`news-impact-forecaster`, which waits on seven unbuilt types.

**It is not Phase A blocking, and that is the reason it stays unbuilt today.**
The three segment bots trade on price, greeks and volume; none of them reads a
news item. The one part outside this feature that is blocked by it is
`opportunity-scanner`'s `news-catalyst-detector`, which waits on `news-item` and
`news-impact-forecast` — the very top of this stack — so it cannot do anything
until essentially the whole subsystem exists. Building 24 parts and seven
external integrations the day before the first live session would be the wrong
trade; recording it as a costed plan is the right one.

---

## 19-28 — the intelligence, knowledge and LLM group, walked 2026-09-06

All ten are **fully running** — every declared part launchable and alive — with
no orphan types, no unlaunchable parts and no starved consumers. Their idle
wires are downstream of a closed trade or an LLM call that has not been made.
`autonomous` carries 2,665 wires, second only to observability.

Because nothing was structurally wrong in any of them, the walk that mattered
here was the crypto scan: which of their parts still reason in crypto's terms.
Across all ten, **three**:

- **`market-event-reader` — a symbol pattern that can never match an Indian
  instrument.** `SYMBOL_PATTERN` is
  `\b[0-9]{0,4}[A-Z]{2,10}(?:USDT|USDC|BUSD|PERP|-PERP)\b`, which matches
  `BTCUSDT` and `ETH-PERP` and nothing this project trades. A delisting or a
  contract change naming RELIANCE or NIFTY would be classified correctly and
  attached to **no symbol at all**.

  It is *not* fixed by loosening the regex, and the part's own comment says why:
  "a looser pattern pulls ordinary words out of prose and attaches an event to a
  symbol nobody mentioned." That is a far worse problem in Indian markets than
  in crypto, because Indian tickers *are* ordinary words — PRESTIGE, CHEMICAL,
  MARUTI. The right shape is resolution against the instrument master, which
  this project already has, rather than a pattern over prose. Recorded rather
  than built, because its input is dead in both directions: its producer
  `exchange-announcement-reader` reports `rows_seen: 0`, so the branch carries
  nothing today whatever the pattern says.

- **`paid-spend-ledger` was counting LLM spend in USDT.** Fixed. The reasoning
  in its docstring was "results are in USDT (RL-028), and so is this" — cost and
  PnL in one unit so they can be compared, which is the right instinct reached by
  making the cost follow the account. It stopped working twice over on this date:
  the account settles in **INR** now, and an LLM provider has never billed in
  either. It says **USD**, which is what is actually charged, and the docstring
  now names the consequence rather than hiding it — comparing cost against PnL
  needs a stated rate, and nothing here converts.

- **`decision-cost-accountant`** is crypto-shaped only through the
  `usdt-pnl-statement` type name and its `net_pnl_usdt` field, which is the
  rename already recorded under feature 8 as a deliberate deferral.

## Item 4 — is 29 foundational features enough?

**Yes, on the evidence of walking all 29.** Nothing found in this audit wanted a
home that did not exist:

- Every type consumed has a producer somewhere in the 29 — the orphan-type scan
  returns **zero**.
- The two genuinely missing capabilities both belong inside features that
  already exist: `broker-quote-bridge` went into `market-data-feed` (built), and
  the Indian order path belongs in `broker-adapter` / `execution-venue-adapter`
  (not built, recorded under feature 3).
- The one feature that is mostly unbuilt, `stock-market-news-data`, is not a
  missing *feature* — it is 24 missing parts inside a declared one, mapped in
  dependency order under feature 29.

The honest qualifier: this answers "does the blueprint have a place for
everything the audit found", which is what item 4 asks. It does not answer
"will a thirtieth be wanted once the bots have traded" — that is a question only
a live session can raise, and Monday is the first one.

## The detectors were calibrated on crypto, and nothing could raise a candidate — 2026-09-06

Walking the 29 features answered "is data flowing" everywhere. It did not ask
the question Monday actually turns on: **can any detector raise an
`entry-candidate` on real Indian prices?** Two instruments say no, and they
agree.

The live spine's own counters (`dashboard/audit_feature_dataflow.py
opportunity-scanner`): `entry-candidate` is **not carrying out of any of the
seven parts that can produce it**. Not one candidate has ever been raised.

Driving the real parts over the real captured NSE tape for 2026-09-04 — 21
instruments, 152,739 detections — reproduces it: **6 candidates**, all on one
thin equity (TMCV), none on anything either options segment trades.

### The cold start has exactly one door

- `momentum-burst-detector` **found 2,129 real bursts** on NIFTY and BANKNIFTY
  and discarded every one at `no-playbook-rule-for-this-regime`. Its z gate is
  fine on Indian data. It needs a `playbook-rule`, which needs an
  `opportunity-instruction`, which needs a closed trade — so it **cannot open a
  cold system**, and the live counters confirm `playbook-rule` has never carried
  into any of its three consumers.
- `spread-reversion-detector`, `volatility-gap-detector` and
  `expiry-day-zero-to-hero-detector` all publish nothing today.
- `mean-reversion-detector` needs no playbook. It is the **only** detector that
  can raise the first candidate on a cold system — and it was the one held shut.

### What held it shut was a crypto fee constant

`mean_reversion_minimum_volatility_fraction = 0.0005`, whose own note read
*"five basis points, just under the round trip at **the venues' taker fees**"*.
That is a crypto perpetual's cost measured against a crypto perpetual's price.

`broker-underlying-price-frame-bridge` publishes exactly **NIFTY, BANKNIFTY and
SENSEX** as `symbol-price-frame`, so those three are the only instruments this
setting can act on live. NIFTY's own standard deviation over a 256-observation
window is **0.000032 of its level** at the median — sixteen times under the
floor. Measured over the real session:

    NIFTY      26,542 in-session prints   24,467 refused as no-volatility (92.2%)   0 candidates
    BANKNIFTY  24,896 in-session prints   23,128 refused as no-volatility (92.9%)   0 candidates

**In-session prints only, and that check mattered.** 24.5% of NIFTY's captured
records fall outside 09:15-15:30 IST: the feed holds its connection through the
close and Upstox restates the last traded price. Those records carry **no new
prices at all** (1,342 distinct in-session, 1,342 distinct overall), so a
256-observation window filled with them has a standard deviation of zero and
refuses for the right reason at any floor. Measured both ways before this was
written down, because a refusal rate inflated by a closed market would have made
the crypto floor look worse than it is: whole-tape 93.3% / 94.2%, in-session
92.2% / 92.9%, and **zero candidates either way**. The conclusion does not
depend on it. The figures quoted here are the in-session ones.

The z threshold itself is **not** the problem: 12.56% of full-window
observations on real Indian ticks reach `detector_z_threshold = 2.0`, and the
reverting regime is reachable on both indices (3,814 and 8,865 readings).

### The right number, derived the way the Indian path really charges

The signal is on the **index**; the trade is in an **option**. So the condition
is not "does the index move more than a fee on the index" — it is

    delta x z_threshold x sd_index  >=  round_trip_fraction x premium

Evaluated against real captured option greeks and real premiums for 2026-09-04,
with `runtime/indian_options_fee_model.py` and the six published Upstox charge
rates already in settings:

    NIFTY 23800 CE   premium 205.00  delta 0.726  round trip 0.541%   needs 0.000032
    NIFTY 23950 CE   premium 101.00  delta 0.516  round trip 0.906%   needs 0.000037
    NIFTY 23800 PE   premium  31.30  delta 0.239  round trip 2.507%   needs 0.000069
    NIFTY 24150 CE   premium  25.70  delta 0.203  round trip 3.012%   needs 0.000080

The median, **0.000037**, is now the setting. Measured after the change, on the
same tape:

    NIFTY      309 candidates    (still refuses 39.9% as no-volatility)
    BANKNIFTY  1,161 candidates  (still refuses 23.3%)

The same 309 and 1,161 on the whole tape and on in-session prints alone -- every
candidate was raised inside market hours, which is the only place one could be
acted on. In-session `sd/mean` is 0.000041 on NIFTY and 0.000055 on BANKNIFTY at
the median, so the new floor sits just under the market's own ordinary
volatility and the old one sat an order of magnitude above it.

**0 → 1,470.** The floor still refuses about half of all observations, so it is
doing real work rather than having been switched off.

### What is still wrong, and is recorded rather than built

The spread across those four contracts is **2.5x**, and it is real rather than
noise: a far-OTM contract's flat Rs20 brokerage is a much larger share of a
small premium. **One global fraction cannot express "the move must pay for the
contract I am about to buy"** — the correct shape is a per-contract bound
computed at strike selection, where the premium and delta are known. The median
unblocks the cold start; it does not make the test right for every strike.

Also open, and larger: `momentum-burst-detector`, `volatility-gap-detector` and
the whole instruction chain behind `procedural-playbook` are dead until the
first trade closes. That is the cold start working as designed, not a defect —
but it means **the first live session's candidates all come from one detector**,
and that is worth knowing before Monday rather than after.

### The measurement that keeps this from recurring

82 of 922 settings still carry provenance naming a crypto venue, instrument or
fee; **77 of those are read by a running part or the runtime**. That is a
number that can fall, and it belongs beside the drift guard's file count.

### The arbiter's unread input, closed

`opinion-arbiter` declared twelve `consumes` types and bound a reader for
eleven. `bot-maturity` appeared exactly once in the file — inside
`PART_DECLARATION` — so it had never affected an arbitration, while R-01 drew a
wire on every diagram no message could travel and the audit board reported
`NOT CARRYING` against a producer that was not at fault.

`apply_2026-09-06_declared_inputs_that_were_never_read.py` deliberately left
this one for the operator, because binding it would change how trades are
arbitrated. Asked and answered 2026-09-06: **drop it**. Maturity is a gate on
what the system may do — which is exactly how `live-switch-guard`,
`autonomy-boundary`, `opinion-conflict-resolver` and `exploration-pair-opener`
use it — not one of the things the arbiter's docstring argues, item by item,
into conviction. Its only producer has never published, so binding it would have
added a code path nothing exercises one day before the first live run.

Blueprint edit first (`docs/proposals/the-arbiter-does-not-weigh-maturity.md`,
`dashboard/blueprint_edits/apply_2026-09-06_arbiter_does_not_weigh_maturity.py`),
then the code. **All four checkers pass together for the first time**, which is
what `check_declared_inputs.py` needed to join the other three in the pre-commit
hook.

### The chain that raises a candidate now runs on Indian prints — 2026-09-06

`tests/integration/test_market_data_to_entry_candidate.py` is the one test where
parts talk to each other as real processes under the launcher —
`price-level-sampler`, `regime-classifier`, `cointegration-pair-finder`,
`spread-reversion-detector` — and it replayed **Binance and Bybit**. Its own
comment said why: *"Porting to the Upstox broker tape is real work and
outstanding — it is a different tape shape with no VenueAdapter behind it."*

That stopped being true earlier in this same audit. `tests/conftest`'s
`upstox_trades_for` runs captured Upstox prints through
`broker-market-data-bridge` — the part the live spine itself uses — so what
comes out is `market-data`, exactly the type `price-level-sampler` already
consumed. **The chain under test did not have to change at all; only the market
feeding it did.**

Ported, and it passes on the real tape: **2 passed in 162s**, raising a real
`entry-candidate` from `spread-reversion-detector` on a real NIFTY option chain.

One deliberate choice inside it: the replay uses `busiest_upstox_option_chain`,
**one underlying's contracts**, not the six busiest contracts outright.
Measured on 2026-09-04, the six busiest NSE_FO contracts spanned four unrelated
underlyings and produced **0 cointegrated pairs**; the six busiest NIFTY
contracts produced **6**. Options on different underlyings have no reason to
move together, so the scanner correctly finds nothing and the test reads as a
broken chain when it is not.

**This corrects the statement above.** "No detector has ever raised a candidate"
is true of the *live spine* — the bus counters say `entry-candidate` carries out
of none of the seven producers — and it was true of `mean-reversion-detector` on
Indian prices. It is **not** true that the detector-to-candidate path was
unexercised: this test exercises it as real processes, and now does so on Indian
data. What remains untested end to end is the half *after* the candidate —
setup filter, opinion, arbiter, intent, order — because
`operate/replay_a_captured_session.py` stamps `replay-opened-at-the-first-print`
and opens without consulting a detector at all. Monday is still that path's
first run.

Drift guard: **107 → 105 files**.

### 61 settings existed only on this VM — 2026-09-06

`settings/runtime.example.toml` is the tracked twin of the operator's live
`~/.config/ajit-segment-bots/settings/runtime.toml`. Comparing them key by key
while re-deriving the volatility floor: the live file held **922** settings and
the tracked one **863**.

The 61 that existed nowhere but this disk are almost exactly the Indian-market
work:

- **all seven `equity_intraday_*` charge rates** — the entire cash-equity fee
  model, brokerage through GST
- **all eight `cash_equity_shortlist_*` weights** and the pool size
- the nine `broker_*` connection, cadence and freshness settings
- `underlying_price_bridge_index_trading_symbols` — the three index underlyings
  the detectors are fed
- the three `zero_to_hero_*` thresholds, and `position_state_root`

Rule 9 exists for precisely this. A tuned number nobody can restore is not
configuration, it is a memory of one machine. All 61 are now in the tracked file
with their provenance notes copied verbatim; the live file stays the one the
parts actually read. Scanned for credential shapes and machine-absolute paths
before copying — none; `position_state_root` is `~`-relative like every path
setting already tracked.

Two settings run the other way (`tail_crowding_funding_deviation_threshold`,
`tail_crowding_sentiment_deviation_threshold`): tracked but no longer live,
both crypto funding-shaped and left in place rather than deleted alongside the
rest of the crypto retirement.

### Three guessed thresholds became measured ones

`zero_to_hero_maximum_premium` (Rs 5), `zero_to_hero_maximum_abs_delta` (0.10)
and `zero_to_hero_horizon_seconds` all carried the same note: *"NOT YET MEASURED
against real captured Indian option-chain data — no live account is connected."*
There is captured data now.

Across **1,524 NSE_FO contracts** on the 2026-09-04 tape carrying both a real
in-session premium and a real Upstox delta:

    premium   p01 0.18   p05 0.70   p10 1.35   p25 5.00   p50 29.90
    |delta|   p01 0.005  p05 0.017  p10 0.034  p25 0.130

**Rs 5 is the chain's own first quartile almost exactly**, and 0.10 delta sits
between its p10 and p25. The two colloquial defaults select the cheapest and
furthest-OTM quarter of a real chain, which is what they were reaching for.
Passing rates: premium 25.3%, delta 21.6%, both 16.3% — and they disagree on
14.4% of contracts, so neither is redundant.

**The values are kept, not moved.** They are sourced now; what should move them
is a measured hit rate on scored candidates, not a re-fit to the same
distribution. The detector only fires on expiry day and 2026-09-04 was not one
for these contracts, so this measures the chain rather than the pattern — said
in the notes so the next reader does not mistake one for the other.


---

## A third temporary goal arrived — 2026-09-06

The user gave a new standing goal while this audit's work was in flight, and it
asks a different question from the one this ledger answers. **This ledger
measures whether data flows.** The new one asks whether what flows is worth
anything — whether each of the 373 parts is *"really providing or working with
the data according to its intended purpose, or if it is just a skeleton
providing or inputting or outputing rubbish data which is not useful just
decorating data"*.

The two are not interchangeable and neither replaces the other. Everything in
this file — the bus counters, `CARRYING` versus `NOT CARRYING`, the four
checkers — proves a message moved. None of it proves the message was right. The
clearest case is in this very session: `mean-reversion-detector` read as a
healthy part on every instrument here, with its inputs carrying and its code
correct, while its output was zero candidates because a crypto fee constant
gated it. Flow measurement could not see that; feeding it real NIFTY prints and
reading the output could.

Its ledger is **`docs/part-purpose-audit.md`**, seeded this day with all 373
parts at `NOT MEASURED`, and its number is section 5 of
`dashboard/measure_objectives.py`. The statement in the user's own words is at
the top of `docs/goal.md`.

### What each part is actually working on — measured 2026-09-06

The operator asked, from the live board, why parts still looked like they were
working on crypto. `dashboard/audit_part_data_reality.py` was written to answer
it for every part rather than by sampling: it resolves the symbols in each
part's own checkpoint against the **real Upstox instrument master**, and takes a
counter delta across two readings of the heartbeat table it observes itself.

**The answer is that almost nothing is on crypto data — and two things are.**

Every symbol-holding checkpoint resolves to real Indian instruments:
`cointegration-pair-finder` 5,949 of 5,949, `regime-classifier` 1,720 of 1,720,
both naming only `upstox`. The two genuine remnants:

- **`paper-account-keeper.paper-account-futures.json` holds `BTCUSDT` on
  `binance-usdm`** — the retired futures segment's paper account, still on disk
  and still restored.
- **`feed-gap-detector`'s entire standing is keyed by crypto venues** —
  `tracked_streams.binance-usdm`, `tracked_streams.bybit-linear`,
  `gaps_found.*`, `messages_seen.*`, `resyncs_seen.*` — and it is IDLE. It is
  watching two dead streams and is not watching the Upstox feed at all, so
  nothing would notice a gap in the feed the bots actually trade on.

**A regex is not allowed to answer this question.** The first pass classified
`788BOBPERP` as a crypto perpetual; it is a Bank of Baroda perpetual **bond**.
Indian debt series (`0MOFSL27`, `1015SCL28B`) and ordinary NSE equities
(`20MICRONS`, `3MINDIA`, `5PAISA`) all read as "not Indian" to a pattern written
around crypto tickers. The master lookup is the only honest classifier, and the
probe uses it.

### The real defect the board was showing

Not crypto — **dead universes that make a part look maximally busy.**

    cointegration-pair-finder   5,949 symbols held, 5,512 holding ONE price,
                                newest observation 2.4 days old,
                                7,814,961 pairs tested, 7,804,577 verdicts
                                suppressed -- and the board reads WORKING 1,619/s
    regime-classifier           1,720 symbols, 1,701 holding one price, 2.4 days old
    corporate-action-adjuster   93,482 instruments known, IDLE
    bull/bear-feature-builder   2,061 symbols each, IDLE

`cointegration-pair-finder` has **no forgetting of any kind** — no
`forget_silent_symbols`, no age bound, no check against what the feed
subscribes — so it restores every symbol it has ever seen (`restored_symbols`
5,926) and tests the square of that universe forever. `regime-classifier` grew
exactly this fix already; the pair finder never did. This is the shape CLAUDE.md
warns about — the pair scanner failing as the square of the universe — arriving
by a different route: not a raised symbol count, but a checkpoint nobody expires.

### Two claims of mine that were wrong, corrected

**1. The detectors do not see only three underlyings.** This ledger and the
`mean_reversion_minimum_volatility_fraction` note both said
`broker-underlying-price-frame-bridge` publishes exactly NIFTY, BANKNIFTY and
SENSEX, so those were the only instruments the floor could act on.
`symbol-price-frame` has a **second producer** — `price-level-sampler`, which
consumes `market-data` and therefore carries every Upstox print the bridge
relays. Measured from the live heartbeat: `mean-reversion-detector` holds
**1,721** symbols, not 3.

That makes the single-number shape of the floor *worse*, not better: the
universe spans indices at `sd/mean` ~4e-5 and options at ~1e-2, **250x apart,
gated by one fraction**. The zero-candidate finding stands — it was measured
from the parts' own bus counters, not from this reasoning — but the
per-instrument bound is now the fix rather than a refinement.

**2. The feed does not subscribe 588 instruments.** CLAUDE.md records "588 of
588" after the subscription was narrowed. Measured live this day:
`broker-market-feed-reader` and `subscribed-instrument-listing-filter` both
report `subscribed_instruments` **1,974**, with `universe_instruments_known`
491. Whatever narrowed it has not held, or 588 counted something else.

### Both crypto remnants fixed — 2026-09-06

**1. The retired futures paper account is gone.**
`paper-account-keeper.paper-account-futures.json` held a live crypto position —
0.064 BTC at 77,511.70 on `binance-usdm`, margin 4,960.75, `fills_applied` 1 —
and was being **re-written**, not merely left on disk: its `saved_at_ns` was that
same afternoon. `built_segments` already excludes futures, so nothing should
have created it; the file was orphaned state from when `segment_id` was
`futures`, restored and re-armed on every restart of the keeper (which the
warden was restarting repeatedly for `taking-longer-every-tick`).

Removed from the live path, with a copy kept in the project's own
`positions-crypto-era-backup-2026-09-02/` rather than destroyed — the same
convention that directory already records. Verified: **it did not come back
through a full spine restart.**

**2. `feed-gap-detector` watches the Indian feed now.**

The defect was two lines. `detectors` was built only from
`load_captured_venue_adapters`, and the tick did `continue` on any message whose
venue had no detector — so every Upstox print was dropped in silence. The part
read IDLE with its entire standing keyed by `binance-usdm` and `bybit-linear`:
watching two streams that stopped on 2026-09-01, and **not watching the feed the
segment bots actually trade**. Nothing would have noticed the Indian feed going
quiet.

Converted rather than deleted (goal 2, item 3). Three pieces:

- **`BrokerFeedContinuity`** — Upstox numbers nothing on its LTP stream, which is
  exactly `SequenceContinuity.NOT_NUMBERED`, a case the vocabulary already
  carried: *"the venue numbers nothing on this stream, so silence is the only
  detector"*. Deliberately **not** a `VenueAdapter`: Upstox is a broker, and
  inventing an adapter to satisfy a type would claim this feed answers order
  books and premiums it does not.
- **Per-stream silence patience.** `feed_gap_threshold`'s own note says the flat
  60 s *"is NOT safe at the full universe: an illiquid perpetual is quiet for
  minutes at a time"*, and 1,974 subscribed NSE instruments are that case at
  scale. The bound is now `max(60 s, 2.8 x this stream's own p99 gap)`, the same
  rule and the same measured multiple `price_gap_patience_multiple` and
  `anomaly_feed_silence_patience_multiple` already carry. Without it this would
  have repeated the flood those notes record — 3,282 of 3,289 anomalies false on
  merely-quiet NSE contracts.
- **The silent `continue` is now counted.** `dropped_no_detector_for.<venue>` and
  a total, so a message thrown away for want of a detector is a figure on the
  board instead of the invisible line that hid this for five days (Rule 8).

**A defect in that fix, caught before it shipped.** The patience estimate first
swallowed the overnight close — one enormous gap every night — and set a
**14,375 s (four-hour) bound on a 6.25-hour session**, which would have blinded
the detector for most of the day it exists to watch. Measured on the quiet
contract `NSE_FO|84322`: p99 inter-print gap 4,742 s across the whole tape
against 1,239 s in-session. Fixed on a principle rather than another number —
**an outage is not evidence about ordinary quiet**, so a gap already past the
bound is reported and then *not* fed back into the estimate that produced it.
Bound fell from 14,375.8 s to **166.5 s**, an 86x tightening, with still zero
false gaps on that contract.

Measured after the restart, on the live spine:

    messages_seen.upstox            5,262      (was 0, ever)
    tracked_streams.upstox              4
    sequence_gaps                       0      Upstox numbers nothing; 0 is correct
    silence_gaps                        2      real
    dropped_no_detector_for_total       0
    messages_seen.binance-usdm          0      kept, so the zero stays visible

Two new settings with provenance: `broker_feed_venue_id` (a setting rather than
an import of another part's constant, T-4) and `feed_gap_patience_multiple`.

---

## 2026-09-07 — why no options order could fill, and why cash equity never tried

Three bots on live NSE data. Stock options had opened eighteen positions all
session, index options none, cash-equity-intraday had never formed a single
trade intent. Two independent causes.

### Cause one: every order was priced on the wrong instrument

`stop-target-placer.priced_entry_for` took the chosen contract's own reference
price **or the underlying's price when there was none**, and `position-sizer`
sizes an open from exactly that number. So orders left carrying the underlying:
`INFY 1200 CE 23 NOV 26` at **1,088.40** against a real premium of **21.10**,
`RELIANCE 1320 PE` at 1,311.05 against 22.85. `paper-fill-simulator` compares
against the contract's market price and refuses past
`maximum_decision_price_drift` (6%), so **6,806 of 8,049** orders that reached a
verdict were refused as `decision-price-stale` — 84.6%.

Index options were worse, and for a reason worth writing down: those intents
*name a contract*, so `select()` resolved the contract to its underlying, picked
the ATM strike, and then priced the strike the **intent** named. `NIFTY 23750 PE`
went out at 3.20 — a deep-OTM wing's premium — while the contract being ordered
traded at 48.75. Drift 1,425%.

**How often the contract had no price, and why.** The believable-age bound is
`(materiality / one-second move)²`, clamped to `[1s, 60s]`, and both inputs were
the wrong market's:

| | in use | measured | out by |
|---|---|---|---|
| `reference_price_prior_one_second_move` | 0.000898, six Binance/Bybit perpetuals | **0.002324** | 2.6x |
| materiality, `2 x taker_fee_rate` | 0.0011, twice Bybit's taker rate | **0.008532** | 7.8x |

The errors ran in opposite directions and cancelled into **1.50 s**, which looks
ordinary. The median live option contract prints every **9.25 s** (p90 66.6 s),
so the newest price in existence was refused **82.0%** of the time — against
`chosen_without_a_price_for_the_contract` measured at **84.5%** (6,981 of 8,259).
Corrected, the same formula gives **13.48 s**. Crossing the spread twice is two
thirds of the real round trip (half spread 0.3175% at p50 over 23,606 book
snapshots; charges 0.2341%), which is why a fee-only figure was 7.8x light.
`measurements/2026-09-07-indian-price-staleness/` carries it.

Fixed on the principle rather than only the numbers: **an unpriced choice
refuses.** `instrument-selector` now gates on the contract it chose rather than
on the intent's symbol — an option chain's underlying always prints, so the old
gate passed exactly when the contract could not be priced — and
`priced_entry_for` returns `None` with `stop-target-placer` counting
`skipped_without_a_price_for_the_instrument`. An order on the wrong scale is
worse than no order: it consumes a decision, a slot and a refusal, and reads on
every board as a bot that is trading.

### Cause two: the cash-equity shortlist had sealed itself shut

`cash-equity-intraday` was subscribed to `3PLAND`, `63MOONS`, `AADHARHFC`,
`ABGSEC`, `ABSLLIQUID` (a liquid-fund ETF), `ABSLBANETF` and 33 more — the
alphabetical head of the NSE master, out of 2,654 candidates. They print 20-700
times a session against MARUTI's 8,002, never filled
`mean-reversion-detector`'s 256-deep window, and raised no candidate at all.
`instrument-selector` recorded **zero** `no-instrument-is-listed-for-this-symbol`
refusals across 162,685 intents: nothing was refused because nothing arrived.

Not the liquidity pool cut, which was the obvious suspect. `percentile_rank`
assigns a **position**, so ranking a missing signal by `entry.symbol` did not
leave candidates tied — it made the alphabet the score, six times over. On the
first ranking of a day nothing has a price and all six signals are `None` for
everything, so the blend *was* the alphabet. The feed subscribed those names,
they became the only names ever measured, and every later ranking returned the
same list.

Fixed by ranking every unmeasured reading by **average daily volume**, which
comes from `equity-historical-profile` and is fetched from history for a share
whether or not it is subscribed — the one liquidity fact that survives having no
subscription.

### Four more found while measuring

- `trade-capital-bounds-gate` `passed` **0**, everything capped,
  `largest_capital_used` 99,999.9999: the operator raised all three
  `segments/*.toml` to 200,000 at 06:00 and `main-account.toml` stayed at
  100,000, so `min()` kept the account ceiling binding and the edit did nothing.
  Same failure as 2026-09-04. `capital-allotment-reader` now publishes
  `binding_ceiling` naming which file won.
- Order quantities were fractional — **12,165.44 units** of a contract the
  exchange trades in blocks of 65. The venue's lot size now travels
  `symbol-universe` → `ListedInstrument` → `InstrumentChoice` → sizer → gate,
  which is the fix `order_quantity_increment`'s own note has asked for since
  2026-08-22.
- `cross-segment-exposure-watch` filed two stock-options positions under
  `by_segment.index-options`: it stamped `segment_id` on every position instead
  of reading `position.segment`, so a cross-segment watch could only ever see one
  segment. Its underlying rule was also `symbol minus "USDT"`, which strips
  nothing from an NSE contract, so every strike was its own underlying.
- `intent_timing_maximum_price_drift` and `limit_walk_maximum_total_fraction`
  were still 0.004 while both notes say they **are**
  `maximum_decision_price_drift`, re-derived to 0.06 on 2026-09-05.

### 2026-09-08 — live spine verification, requested by the user

Checked the trade board's open-positions table live, on the user's request.
10 of 16 open stock-options positions read `NOT MEASURED: the tape has no
record for this symbol today`, real capital in every one.

- **Fixed:** `broker-market-tape-writer` wrote the tape under Upstox's raw
  `instrument_key` (`tape/upstox/NSE_FO|56316/...`); every reader looks it up
  by `trading_symbol`. Now resolves via `broker-subscribed-instrument-listing`
  before writing (`unresolved_writes` counts what still can't resolve).
- **Fixed:** `broker-symbol-universe-bridge` could drop an already-held
  position out of the universe once its strike/expiry aged out of the
  nearest-expiry ranking window meant only to bound new buys. Now reads
  `position` and forces every held symbol's listing in unconditionally.
- **Fixed, found while verifying the first two:** even with the tape
  correctly resolved, the board still read `NOT MEASURED` for everything.
  `dashboard/build_trade_board.py`'s `read_last_price`/`read_price_window`
  called `load_venue_adapter("upstox")` — `runtime/venues/` only has
  `binance_usdm`/`bybit_linear` (`captured_venues` still names only those
  two crypto venues); Upstox was never added there and lives under a
  different class (`runtime/brokers/broker_adapter.py`, no `read_trades()`).
  The load always raised, caught by a bare `except Exception: return None`,
  so a crash read as "no record". Fixed by decoding the tape's own JSON
  (`LtpUpdate`) directly for `venue_id == "upstox"`, bypassing the crypto
  adapter registry entirely. Verified against today's live tape (AXISBANK,
  INFY contracts resolve real prices) and 59/59 `test_build_trade_board.py`
  passing. **Not yet verified through the live board API itself** — see next.
- **Found, not fixed — separate, real, currently live:** `/api/board`
  (`dashboard/part_health_api.py`) hangs under load. Confirmed by direct
  `curl` (>100s, no response) and by an in-page `fetch()` from a real
  browser (`document.hidden` false, ruling out the headless-tab-visibility
  explanation) — both hung. Cold start (`warm_caches()`, synchronous before
  the port opens) measured **285s** against 373 parts; the code's own comment
  still assumes "about ten seconds" at 327. Once warm it answered twice in
  ~10ms, then hung again under a handful of concurrent requests (13
  established connections, 28 threads stuck). Blocks the *entire* live
  frontend, not just Trading — `App.jsx` gates every tab behind `useBoard()`'s
  data even though Trading/Machine/Settings only need their own endpoints,
  which answer fine on their own (`/api/trades` 10ms, `/api/activity` fast).
  Not investigated further this session. `/api/board`'s current caching
  strategy needs to either coalesce concurrent rebuilds or move `warm_caches`
  off the accept path; `App.jsx` should not block unrelated tabs on it either
  way.
- **Fixed — zero-to-hero / NIFTY expiry day:** traced to completion. 2026-09-08
  (Tuesday) is a genuine NIFTY weekly expiry day —
  `expiry-day-zero-to-hero-detector` correctly found 176 instruments expiring
  today and fired 1,643,296 of 3,559,693 detections into real
  `entry-candidate`s. But `index-options`'s paper account showed 2 fills
  *lifetime*, 0 open. Root cause: `instrument-selector`'s
  `_refresh_atm_instruments` registers exactly two candidates per underlying
  — the current ATM call and put — and nothing else. When a trade-intent
  names a specific far-OTM contract (the detector's whole thesis: cheap
  *because* it's far from 0.5 delta), `select()` resolves it to its
  underlying via `contract_named()` and then picks from whatever is listed
  there, which was only ever the ATM pair. The exact contract the detector
  wanted was never a candidate — every such trade was silently the opposite
  bet (highest premium, ~0.5 delta) from the one it was supposed to be. The
  59.7% "no recent trade/quote" refusal rate traced earlier is a separate,
  real, still-open number (mixed across all segments, not isolated to
  zero-to-hero) — not the cause of this one.

  Fixed by teaching `AtmStrikeTracker` to answer for one exact contract
  (`contract_by_symbol`, new) instead of only the nearest-to-0.5-delta pick,
  and having `instrument-selector` register the named contract alongside the
  ATM pair — not in place of it — whenever an intent resolves to one. The
  existing cheapest-cost comparison (unchanged) decides which one actually
  carries the intent, so a genuinely cheap far-OTM contract wins on its own
  economics. `parts/segment_bot/instrument_selector.py`,
  `runtime/atm_strike_tracker.py`. 4735/4739 of the full suite passing (the
  4 failures: 2 were this same investigation's own pre-existing
  `broker-market-tape-writer` PART_DECLARATION order mismatch, fixed
  alongside; 2 are unrelated and pre-existing — confirmed by stashing and
  re-running against the branch before today's changes).

Board restart deliberately not forced to pick up the `build_trade_board.py`
fix — it would hit the `/api/board` hang above, and that's a separate
problem to solve first, not paper over with a lucky restart.

**Fixed, partially — `/api/board` hang.** `BOARD_FRESH_FOR_SECONDS` was 30.0
against a cost measured today at 71.1s quiet (373 parts; the comment beside
it still said "about eleven seconds" from 327). Not a stampede —
`MeasuredCache`'s coalescing lock always worked — every caller was correctly
queued behind one refresh that had become slower than the window meant to
protect it. Raised to 180s. Verified after restarting `ajit-board`: 5/5
requests fast (0.1-5.5s), and the live board's Trading tab — gated behind
`/api/board` for every tab regardless of whether that tab needs board data —
rendered end to end with real prices, confirming this fix and the
`build_trade_board.py` fix above both live in production. **Does not fully
close it**: re-checked minutes later at load average 82-87 on 12 cores (the
373-part spine's own real steady-state cost, confirmed via `vmstat` and
`ps`, unrelated to this session), and even a 200s timeout wasn't enough. A
cache-window fix cannot buy CPU the box does not have spare — that's the
machine-capacity tension RL-072 already tracks ("the board should show parts
off with the reason, not a coverage number that quietly excludes them"), not
something to solve here.

**Still open for a future session:** the 59.7% `this-symbol-has-no-recent-
trade-and-no-recent-quote` refusal rate across all segments today — real,
not yet traced to a cause, and now that zero-to-hero's own contract is
actually a candidate, worth re-measuring first to see how much of that
number was this bug's own downstream shadow (a contract nobody could ever
really price because it was never the one being asked about) versus a
genuine live-price gap. And, larger: whether `/api/board`'s per-part
filesystem scan can be made cheaper, or moved off the request-serving path
entirely (background refresh thread), rather than only widening the window
around it — the 373-part cost will only grow as more parts land.

### 2026-09-08, later — "also no new trades did not open today"

The user's own words, mid-session. Confirmed real via the position journal:
last position opened 2026-09-07T09:44 UTC; nothing opened all of today's
session (03:45 UTC onward, ~4 hours in when checked).

**Root cause, traced with a temporary diagnostic on `position-sizer` (added,
verified, removed):** every actionable trade-intent the live spine formed all
session was `action: "close"` — a wide 80MB journal sample found 4,483
actionable intents and **zero** were `open`. `position-sizer` sized every
intent through the entry/stop risk-budget path built for OPEN, and a CLOSE
carries neither by construction (`bull-opinion-composer.compose()` refuses to
form an opinion without a complete `exit_plan`, and there's nowhere new to
enter when closing). Confirmed live: a real close (`ICICIBANK 1440 PE 29 SEP
26`) priced correctly (`reference_price=22.525`) but then fell through to
`intent.stop_price = None` — refused as `missing_stop_price`, 100% of the
time, every close, all session. Separately, `opening_order_target()` only
checks the instrument choice for `action == OPEN`, so
`opens_without_an_instrument_choice` stayed 0 throughout and never surfaced
the real gap.

**Fixed** (`docs/proposals/position-sizer-closes-what-is-held.md`):
`position-sizer` now consumes `position` and sizes CLOSE/REDUCE through a new
`close_order()` — quantity from the held position, side opposite what's held,
no stop or risk budget. `trade-capital-bounds-gate` now skips its
capital-ceiling economics for a close (it was checking a closing order's
notional against the position's own already-committed capital, backwards for
an order that reduces it). Both `SizedOrder`/`BoundedOrder` carry a new
`action` field. 10/10 new unit tests pass, full `risk_capital_allocation`
suite (294) passes, contracts hold.

**Two more real defects found and fixed while deploying it:**
- `trade-capital-bounds-gate` crash-looped (`AttributeError: 'NoneType'
  object has no attribute 'minimum_capital'`) the moment it was restarted —
  pre-existing, not caused by this change: `_bounded()` read `bounds.*`
  unconditionally even on the `REFUSED_SETTINGS_INVALID` path, which is
  reachable with `bounds=None` on any cold start before this segment's
  `trade-capital-bounds` has ever been read. Fixed with `getattr(bounds,
  ..., 0.0)`. 11 crash-restarts before caught.
- **Per-part restart does not add a new bus subscription.** After
  individually restarting `position-sizer` (picking up the code, including
  the new `position` consumption), its own `messages_received` never showed
  a single `position` message despite `fill-reconciler` publishing 540,600
  of them. Bus subscriptions (file descriptors) are wired once when the
  whole spine starts; a per-part kill+respawn re-execs the same part with
  the same wiring the spine handed it at spine-start, not a fresh one
  matching the part's current `consumes`. Needed a **full spine restart** to
  actually wire the new type in — confirmed after: `position` received
  (3,310) within minutes. Worth remembering for every future
  consumes-widening change: a per-part restart proves the code runs: it does
  not prove the wire exists.

**Verified partially, not fully end-to-end:** as of this entry, no real
(non-stand-aside) trade-intent has formed since the full spine restart, so no
close has yet been observed completing through the fixed path live — the
bull/bear conviction pipeline needs to warm up again, same as after the
earlier restarts today. The fix is structurally verified (unit tests, no
crashes, `position` wire confirmed flowing) but not yet watched carry a real
order through `bound()` → `paper-fill-simulator` → a closed position.

**A fourth, separate issue found while waiting — traced deep, not fixed, needs
a design decision rather than a number.** `bull-opinion-composer` stood down
100% of 810 opinions this restart (`conviction-below-threshold`), with
`last_floor: 1.0` pinned — sustained, not transient.

Traced the math exactly. `_cold_start_targets` makes reward-to-risk
*structurally constant* — `reach = stop_fraction * multiple`, so
`stop_fraction` cancels out of `reward_to_risk = Σ(multiple·fraction)`.
With `bull_cold_start_reward_multiples=[1,2,3]` and
`bull_exit_target_fractions=[0.4,0.4,0.2]`: R = 1×0.4+2×0.4+3×0.2 = **1.8**,
fixed, always. So `break_even = (1+cost)/(1+1.8) = (1+cost)/2.8`, and
`cost = 2·fee_rate/stop_fraction` with `fee_rate =
per_side_trading_cost_fraction = 0.4266%` (correctly re-derived for NSE
options, 2026-09-07). Floor clears (< 1.0) only when `stop_fraction >
0.474%`; comfortable headroom (floor ≤ 0.85) needs `stop_fraction ≥ 0.62%`.
`stop_fraction = bull_cold_start_stop_range_multiple (1.5, still dated
2026-08-23) × live_range_fraction`.

**First hypothesis, measured and refuted:** guessed the live-range
measurement itself was too tight for illiquid options and re-derived from
real captured tape data — 60 real option contracts, 19,723 real prints,
horizons 30-300s. Median range fraction 1.2-2.9%, far more than enough to
clear the floor at the *current* 1.5x multiplier. Contradicted the live
100% stand-down rate, so the hypothesis was wrong, not the fix.

**Root traced instead: `candidate.symbol` for the opinions actually forming
today is the *underlying stock*, not an option.** Checked real
entry-candidates in the journal: `volatility-gap-detector -> AXISBANK` (bare
stock symbol, no CE/PE) is what feeds `bull-feature-builder` →
`bull-conviction-model` → `bull-exit-plan-proposer`, so
`live_range_fraction` measures **the stock's own price range**, not the
option's — stocks move far less per second than options do (no
leverage/gamma), so the cold-start stop this segment computes is
structurally too tight almost regardless of the multiplier, while it is
compared against `per_side_trading_cost_fraction`, a cost **derived
specifically for the options segment's real spread and fees**.

This is not a settings typo to correct — it is a real question about which
space the cold-start stop should be measured in when a directional bot's
underlying-based signal is expressed through an options-segment instrument
(index-options, stock-options) versus directly (cash-equity-intraday, where
underlying-range and traded-instrument-range are the same thing and this
mismatch does not exist). Left open for the next session, with the exact
math and the refuted hypothesis both recorded here so neither has to be
re-derived: should the cold-start stop be measured against the chosen
instrument's own range once one exists (circular — instrument-selector runs
after the opinion is formed), against a segment-specific fee/cost pairing
matched to what actually produced the range (stock fee for a stock range,
option fee for an option range), or something else. A decision for the
user, not a number for Claude to pick.

---

## 2026-09-08 — the conviction floor answered, and all three segments walked

The user: *"fix the last session finding on the issue"*, then *"fix all the
design questions and errors of no new trades opening in 3 segments"*, then
*"intraday cash stocks did not open once with leverage"* and
*"cash-equity-intraday … see zero trades opened find the reason"*.

**The state this session opened in.** Every trade intent the live spine formed
that day was `stand-aside`: 9,085 of 9,085, 100% for
`conviction-below-threshold`, both bots, with
`bull-opinion-composer.last_floor` pinned at 1.0. Nothing downstream of the
arbiter had been exercised at all.

### 1. The conviction floor was two defects wearing one symptom

**A name.** `expiry-day-zero-to-hero-detector` published Upstox's
`instrument_key` (`NSE_FO|42631`) as the candidate's `symbol`, while
`market-data`, `symbol-price-frame` and the tape are all keyed by
`trading_symbol` — `broker-market-data-bridge` resolves the key on the way in.
It raised **494,125 of the bull bot's 498,952** accepted candidates, and:

    bull-exit-plan-proposer   216,227 of 218,520 refused no-price-for-this-symbol   99.0%
    bull-entry-timer          493,388 of 498,056 stood down, same reason            99.1%

The 837 plans that *did* get built therefore came from the other three
detectors, which name **underlyings** — and that is what pinned the floor.

**A space mismatch, which was the design question 2026-09-07 left open.**
`ConvictionFloor` divides a round-trip cost by a risk fraction, and the ratio
means nothing unless both are fractions of the same instrument's price. Every
gate passed `per_side_trading_cost_fraction` — derived for an NSE **option
premium** — against risk fractions measured on a **stock**.
`widest_live_range_fraction` was 1.01% across all 837 plans, so even the widest
stop gave a floor of 55.8% against a model reporting 50.0%.

Resolved by measuring both on the same instrument:
`liquidity-grade.round_trip_cost_fraction` (that symbol's own book, spread plus
the walk on each side) plus `broker_charge_stack_round_trip_fraction`. Grades
expire on `liquidity_grade_maximum_age_seconds`; a symbol nothing has graded
falls back to exactly what it was charged before. A book that costs 192% to
cross still pins the floor at 1.0, and that is the right answer, not a clamp.
`runtime/symbol_round_trip_cost.py`,
`docs/proposals/the-floor-charges-the-symbols-own-cost.md`,
`measurements/2026-09-08-conviction-floor-name-mismatch/`.

**Measured live the same session, with only the naming fix deployed:**

| | before | after |
|---|---|---|
| `bull-opinion-composer` calls to act | 0 | 47 |
| `bear-opinion-composer` calls to act | 8 | 7,784 |
| `opinion-arbiter` intents formed | 0 | 27,484 |
| `paper-fill-simulator` filled | 0 | 56 |
| `fill-reconciler` fills applied | 0 | 88 |
| `position-close-detector` trades closed | 3 | 13 |
| `bull-exit-plan-proposer` no-price refusals | 99.0% | 0 of 546 in the first window |

### 2. cash-equity-intraday: why it opened zero trades, ever

`broker-symbol-universe-bridge` published **every** non-contract with
`instrument_kind=None`, shares included. Run against the deployed segment
settings:

    SPOT IFCI (shortlisted)         -> cash-equity-intraday
    None IFCI (what was published)  -> unknown

`unknown` is `UNKNOWN_SEGMENT`, so the selector answers
`the-best-instrument-is-in-a-segment-that-is-not-built`. Worse, a kindless
listing matches the *"an underlying: what every contract on it resolves
through"* branch, so each share went into the ATM strike tracker instead of into
the book of instruments an intent can be expressed through. The bridge published
47 shares and `instrument-selector.chosen_by_kind` held **17,089 `option` and
nothing else**.

The selector's own SPOT branch — written 2026-09-07 precisely so an equity
intent could be expressed — was unreachable code. **Both halves were tested and
the join between them was not:** the selector's test typed a
`CapturableSymbol(instrument_kind=SPOT)` by hand, and the bridge never produced
one.

Shares are published as `SPOT` now; an index stays kindless, because NIFTY is a
number and not something anyone can buy. The two groups are disjoint already —
`entries()` skips any share a derivative is written on, which is the
double-exposure rule this universe already carries. A new test drives the real
bridge into the real selector and asserts a chosen share.

Leverage was never the problem: `leverage-selector` chose 5x, 22 times,
`held_at_ceiling` on every one.

### 3. Three more defects, all found in the same live session

- **`broker-margin-quoter` was rate-limiting itself.** 18,943 of 18,946 calls
  failed, every one HTTP 429 `UDAPI10005 Too Many Request Sent` — reproduced by
  hand against the real endpoint. A failed call marks nothing as quoted, so the
  same instruments were due again on the very next tick and the retry *was* the
  cause. `unattended-run-warden` escalated it four times as
  `taking-longer-every-tick`. After the backoff, measured live: **2 calls made,
  2 refused, 1,183 ticks waiting it out.**
- **Every close sold the whole position, over and over.** 12,168 closes against
  16 open symbols; `position-close-detector` refused 10,005 units as an
  unmatched exit and `paper-account-index-options` held **-1,042.29** of
  `NIFTY 23650 CE 15 SEP 26` — a short option written in a buy-only segment. A
  close is not restated while one for the same held quantity is outstanding; a
  quantity that *changes* re-opens the ask immediately, because a partial fill
  deserves its own smaller order.
- **Fractional option quantities.** `paper-account-stock-options` held
  **12,207.81406719471** units of `AXISBANK 1260 CE 29 SEP 26`. The
  option-listing path never recorded the venue's lot size, so the global
  `order_quantity_increment` (0.001) applied — the same defect fixed once on the
  `symbol-universe` path on 2026-09-07, in the other doorway.

### 4. The exit-plan starvation, corrected rather than waived

`bull-exit-plan-proposer` refused **84,838 of 86,943** requests as
`too-few-prints-in-the-window-to-measure-a-range`; the bear peer 71,211 of
77,282. The bar was 20 prints, measured on BTCUSDT at ~4 prints a second; the
median NSE option prints five in a minute.

Re-derived on this project's own tape — 300 symbols, 1,974 windows, 9,070 draws
per sample size — the share of a window's true range a sample of n prints spans
at the median is 41.4% at 3, **60.2% at 5**, 73.0% at 8, 89.2% at 20. It
reproduces the 2026-09-07 table whose script was never saved, independently.

So the bar drops to 5 **and** the range is divided by the recovery at its own
sample size (`runtime/range_from_a_small_sample.py`). Lowering it alone would
have placed stops ~40% too tight, which is exactly why 2026-09-07 refused to
move it. The **median** is used and not a lower quantile, because
over-correcting is not the safe direction it looks like: a wider stop is a
larger risk fraction, which makes the round trip cheaper in units of risk and so
*lowers* the conviction floor.
`measurements/2026-09-08-a-range-from-a-small-sample/`.

### Still open after this session

- The detectors' **horizons** are still the crypto ones for three of four
  (`spread_reversion_horizon` 60 s, `mean_reversion_horizon` and
  `momentum_burst_horizon` 300 s). Correcting the estimator makes a short
  horizon survivable; it does not make it right. `signal-horizon-profiler`
  measures what would settle each.
- `broker_charge_stack_round_trip_fraction` is the **options** stack charged to
  every symbol; on a cash equity it overstates the charges, which raises the
  floor and so refuses rather than over-trades. It stops being an approximation
  when `liquidity-grade` carries the instrument kind.
- `position-sizer.opens_without_an_instrument_choice` reached **5,268 of 5,268**
  actionable intents in the session's last window. Traced far enough to find
  that **the counter was hiding its own diagnosis**: `instrument-selector`
  publishes every verdict, refusals included
  (`publish_choices(tuple(select(intent) for ...))`), so a choice carrying
  `chosen=None` is the selector saying no *with a named reason* — and the sizer
  counted that identically to no choice ever arriving. Two faults, two different
  parts to go and look at, one number. Split now into
  `opens_the_selector_refused` with the selector's own reason beside it, which
  is what will name the real cause next session; the selector's own refusals
  that day were led by `this-symbol-has-no-recent-trade-and-no-recent-quote`
  (10,612 of 30,517 intents seen). The underlying leak is **not** fixed — only
  made visible. The selector's own docstring already states the rule this broke:
  *a refusal that cannot be seen from outside is indistinguishable from an input
  that never arrived.*
- **`signal_label_move_fraction` is still the crypto-era barrier (0.002), and
  the bound derived from it has collapsed onto its floor.** Found by completing
  a test stub that had been raising `KeyError` — and therefore asserting nothing
  — since `price_staleness_from` began reading
  `reference_price_materiality_fraction` on 2026-09-07. On the crypto numbers
  the trading bound is 1.50 s and the labelling bound 4.96 s, which is the
  property the test protects: the labeller judges against its own barrier, wider
  than a fee. On the numbers actually deployed for NSE the trading bound is
  13.48 s and the labelling bound is **1.00 s, its floor** — a claim is marked
  right or wrong on a 0.2% move when a round trip on an NSE option costs 0.8532%,
  four times more. `signal-outcome-labeller` produces `training-label`, which
  every conviction model learns from, and those models show it:
  `bull-conviction-calibrator` reported `fitted_calibrations 0` against
  `passed_through_unfitted 448,423`, and `bull-conviction-model`
  `mean_absolute_training_error 0.496` on 97,420 labels — a model that has
  learned nothing, whose output never leaves the coin flip the conviction floor
  then has to be cleared from. Re-deriving that barrier for NSE is its own
  change with its own evidence; the test now names the collapse rather than
  asserting a number nobody derived.
- `tail_crowding_funding_deviation_threshold` and
  `tail_crowding_sentiment_deviation_threshold` are in the example settings and
  **missing from the deployed file** (pre-existing, not touched here).
- `paper-account-futures` still holds BTCUSDT on binance-usdm. The audit said it
  was removed 2026-09-06 and did not return through a restart; it has returned.
- cash-equity-intraday's fix is proven by unit test and by the real segment
  resolver, **not yet live** — NSE was closed when it landed. Monday's open is
  its first real test.

---

# Re-walk — 2026-09-12, market shut (Saturday)

The operator asked for the 29 to be walked again. Warranted rather than
ceremonial: since the 2026-09-06 walk the project has **retired a segment**,
**derived both option universes** instead of listing them, **converted 45
settings** off crypto, **retired five learned checkpoints**, and **built a new
part that can spend real money**. Every one of those moves the dataflow picture.

## Where the walk stands now

| | 2026-09-06 | 2026-09-12 |
|---|---|---|
| features walked | 29 of 29 | **29 of 29, re-walked** |
| parts declared | 373 | **374** (`broker-order-router` added) |
| parts launchable | 348 | **349** |
| parts running | 322 | **324** |
| wires carrying | 3,806 of 5,843 (65.1%) | **4,073 of 5,904 (69.0%)** |
| blocks fully on | — | **20 of 29**, 0 dark |
| parts silent | 1 | **0** |

**Counters are ~4 minutes old** (spine restarted 2026-09-12 11:55 UTC for the
fetcher fix below) and NSE is shut, asked of `market-session-calendar` rather
than assumed. Every "idle" reading downstream of a trade is therefore idle for
the honest reason, not a defect.

The instrument's own cross-check is clean:

    Consumers receiving nothing from a producer that is publishing:
      none — every live producer's messages are reaching their consumers

That is the sentence that matters. Idle-because-nothing-is-trading is a
different fact from a producer publishing into a void, and only the second is a
defect. There are none.

## What this walk found

**One defect, and it was invisible to every other check.** The coverage probe
reported one silent part. `book-and-paper-fetcher` fetched EVERY gap that had
arrived in a single tick, one real HTTP call each — 307 gaps seen, 20 fetched,
287 rate-limited, and the part reporting `silent` for 308 seconds with its own
state still `on`, because one tick had been running the whole time.

A part that blocks its own loop for five minutes **cannot be switched off**: the
governor owns the switch and can only take a part that returns to its loop
(T-2). It cannot emit health, so `failing-part-detector` reports it silent while
nothing is wrong with it. And its `skipped_tick_effect` is `corrupts`, which
makes a tick it never finishes worse than one it skips. Bounded to one fetch per
tick, as a setting with provenance. **325 of 325 reporting afterwards, 0
silent.**

No other check could have caught it. All four contract checkers pass on that
part and always did; its wires all carry; the part monitor shows it RUNNING.
Only "how long since this part last spoke" exposes it — which is what this walk
measures and nothing else does.

## The features this session's work changed, verified

| feature | reading | what it confirms |
|---|---|---|
| `market-data-feed` | 14/24 running, **301 carrying, 0 idle between running parts** | the derived universe carries: `broker-symbol-universe-bridge` 4/4 in, 2/2 out. The 10 off are the crypto feed cluster |
| `broker-adapter` | 10/10 running, 194 carrying | **`broker-order-router` running with its three GATE inputs carrying** — `broker-token-standing`, `money-mode`, `symbol-universe` — and idle only on `order-request`, `cancel-decision`, `order-reprice` |
| `portfolio-state` | 6/7 running | `inr-pnl-accountant` running under its new name, 3/7 in; the four idle are `closed-trade`, `fill`, `paper-currency-rate` and crypto `funding-settlement` |
| `execution-venue-adapter` | 2/11 running | unchanged and correct: the crypto real-money path, with `ccxt-order-router` off and its Indian replacement now live in `broker-adapter` |

**The router's gate inputs carrying is the specific thing worth having
measured.** The spine comment written when it was added claims it must start
after those three producers, because "a router whose gates have no input refuses
everything for the wrong reason — which looks identical to refusing correctly".
This walk is the measurement that says the gates are actually fed.

## Still true, still outstanding

- `stock-market-news-data` is **5 of 29 parts running**, 338 wires unmeasured —
  24 parts carry no `start_part` at all. Unchanged since 2026-09-06 and the
  largest single hole in the diagram.
- `ledger`, `opportunity-scanner`, `paper-live-trading`, `prediction`,
  `risk-capital-allocation`, `skills` each have one part off; every one is
  either crypto-only or downstream of a trade.
- The idle wires concentrate where they should: `ai-brain` 176, `learning-loop`
  167, `closed-trade-decoding` 157. All of it is downstream of a closed trade,
  and no trade has closed on a live run since the segments were re-scoped.

## 29 — `stock-market-news-data` again, 2026-09-12: the chain has a head and a first layer

The 2026-09-06 walk found 5 of 29 built and, more usefully, that **nothing
produced `raw-news-item` at all** — so the fourteen parts below a source were
starved at the top of the chain and building any of them first would have
measured nothing. Both ends of that have moved:

| | 2026-09-06 | 2026-09-12 |
|---|---|---|
| parts built and running | 5 of 29 | **10 of 29** |
| producers of `raw-news-item` | 0 | 1 (`broker-news-reader`, Upstox's own News API) |
| consumers of `raw-news-item` | **0** | 4 — deduplicator, tape writer, latency meter, source health monitor |

Every number those five act on is derived from real Upstox articles, captured
twice: `tests/captured/upstox/2026-09-12-news-for-two-underlyings.json` (17 rows,
14 stories) and `-for-thirty-underlyings.json` (42 rows, 31 stories across 167.08
hours). The broker states its own `total_records`, which is what lets the
deduplicator be checked against the source's count rather than against one a test
invented.

**Three defects, and the two that matter were invisible to the test suite:**

- The reader's live standing read 104,646 instruments known and 11,555 failures
  against 9,141 requests at 3.37 a second, with every test green — the running
  process predated its own filter and pace by three minutes. **A part's tests
  pass on the code on disk; the spine runs the code it imported.** After a
  restart: 0 failures, paced, 4,370 listings refused as things no story can be
  about.
- `news-source-health-monitor` counted rows as arrivals, so one poll of NSE's
  F&O ban list (244 reports) made the source look like it delivered twice a
  second, and thirty seconds of ordinary quiet beat every gap on record: two
  sources read `NOT_DELIVERING` inside three minutes of a healthy spine. **A
  delivery is an occasion, not a row** — at most one per source per tick.
  Measured after: 284 rows, 10 deliveries, zero false verdicts.
- The nearest-rank 95th percentile of *n* observations **is** the maximum until
  n ≥ 20, so on the narrower capture the monitor's middle state was unreachable
  — a Rule 8 defect found by a test failing rather than by reading code, and the
  reason a second wider capture was taken.

**What is still dark, measured rather than assumed:** the three types those parts
produce — `distinct-news-item`, `news-latency-reading`, `news-source-standing` —
now have producers and no consumers. `news-text-structurer` (the block's single
LLM read), `news-symbol-resolver`, the classifiers, the scorers and
`news-impact-forecaster` are the next layer, and 19 of the 29 are still unbuilt.

One fact from the tape worth carrying forward: **this source is a digest, not a
wire.** The freshest of 31 real stories was 21.5 hours old when first seen, and
`news-latency-meter` says so rather than a part downstream assuming news arrives
in seconds.

## Two findings from 2026-09-12 that are not about news, recorded so they are not re-found

### The LLM foundation had never made a call, and four defects were why

Sixteen parts publish `llm-request`; none had ever produced one. Not broken —
deadlocked. The whole ring, each part's own counters, and the fixes are in
`measurements/2026-09-12-the-first-llm-call-this-project-has-made/` and
`docs/proposals/the-first-prompt-for-a-purpose-cannot-be-scored.md`. In short:
nothing could promote a purpose's *first* prompt version; no part could get its
*first* budget; `llm-request-router` looked a per-part budget up by
`str(rendered.context_id)`, which is never a part id; and the budget window
rolled on every tick, so no part could ever be found out of budget. Live after:
`purposes_with_an_active_version` 0 → 2, `refused_no_active_version` 906 of 906 →
2 of 41, `budgets_issued` 0 ever → 1,774, `windows_rolled` 855 → 0. The transport
itself was proved with one real call: 20,341 input tokens, 128 out, 4.99s.

**Still one step short of a call through the parts**, and the reason is correct:
`context-assembler` needs a verified snapshot and
`ground-truth-snapshot-builder` refuses to build one while the market is shut.
Monday's open or a purpose-built integration test over captured prints is what
closes it.

### 100% of today's tape records are written under an instrument key — MEASURED, cause not yet confirmed

`broker-market-tape-writer` on the live spine, 2026-09-12:

    records_written    3,935
    unresolved_writes  3,935   -- every single one
    symbols_resolved      39   -- against 3,464 open tapes

and on disk, `3,817` of today's tape directories are named `NSE_FO|105892`-style
instrument keys and **zero** are named trading symbols.

This is the 2026-09-08 incident's exact shape: that day ten open stock-options
positions with real capital had a live tick every second under
`tape/upstox/NSE_FO|56316/...` and read `NOT MEASURED` on the board, because the
board asks for `tape/upstox/AXISBANK 1260 CE 29 SEP 26/...`. The writer was
changed to resolve `instrument_key` to `trading_symbol` through
`broker-subscribed-instrument-listing`, and to fall back to the key rather than
drop a tick — `unresolved_writes` exists to make a resolution gap that never
closes visible. It is not closing.

**What is not yet established:** today is a Saturday, so the records were not
written from a live subscription, and the resolving listing may simply not travel
on whatever path feeds the tape while the market is shut. That would make this a
gap in the shut-market path rather than a regression in the live one. It needs
one reading at Monday's open to tell those apart, and that reading is the next
thing to take: if `unresolved_writes` is still 100% with the market open, every
price lookup by symbol is broken again.

