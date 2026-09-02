# Stock market news data — design

Given by the user 2026-09-02: add a new foundational feature to the diagram,
`stock-market-news-data`, holding the news the Indian market runs on, divided by
segment — index options, stock options, index futures, stock futures, cash
equity, commodities — and wired into every feature that needs it.

This is a blueprint change first (`dashboard/blueprint_edits/`), code second.
Nothing here is implemented until the registry carries it and
`dashboard/check_contracts.py` passes.

## Why the block exists

The blueprint before this change carries 28 blocks, 336 parts, 292 data types
and **no news at all**. Three parts gesture at it and none of them work:

| part | block | what it actually reads |
|---|---|---|
| `market-event-reader` | intelligence | only `venue-announcement` — a crypto venue notice |
| `event-risk-limiter` | risk | `market-event`, which nothing real produces |
| `exchange-announcement-reader` | online-research | crypto listings, delistings, maintenance windows |

For the Indian market that is a hole in the substrate, not a missing nicety.
Three of its facts are not sentiment at all — they are conditions the executor
cannot legally or physically trade through:

- **F&O ban / ASM / GSM / circuit / halt** — an order on a banned name is
  rejected by the exchange, and `order-resubmitter` would retry it forever.
- **Corporate actions** — a 1:1 bonus halves the price. Unadjusted, that is a
  −50% candle: `kline-window-builder` feeds it to Kronos, every detector fires,
  and `cost-basis-tracker` prices a position against a basis that no longer
  exists.
- **Session state** — holidays, expiry-day shifts, special sessions.
  `paper-fill-simulator` will otherwise fill an overnight order at the last
  price it saw and journal it as a trade.

The rest — filings, results, RBI, press, chatter — is what moves the price
between candles. Kronos reads candles only; a scheduled RBI decision is
invisible to it by construction.

## Decisions taken in the interview (2026-09-02)

| | decision |
|---|---|
| **D-N1** | **One global block, segment-tagged output.** Not six per-segment copies. A source is fetched once; every item carries `segments[]` and each segment bot filters its own tag. Six copies would pull the same NSE feed six times and burn six times the rate limit. Joins the resource governor and observability as a stated exception to the per-segment split (`segments.scope_decision`). |
| **D-N2** | **All five source families are in scope**: exchange/regulator official, financial press, social chatter, broker-supplied, plus an autonomous web searcher that goes and looks things up rather than polling a fixed list. |
| **D-N3** | **The block judges as well as delivers** (RL-060). It publishes structured facts *and* a learned direction/impact/confidence, trained on realised price reaction. The LLM read of a headline happens **once** here, not six times in six segment bots, and impact is learnable only against price. Each bot still weighs the score its own way. |
| **D-N4** | **Three separate channels, never one wire.** Hard facts, scheduled events, and scored news signal are different data types with different consumers. This repo has already paid for the alternative: `market-data` carried trades, candles and books on one wire until 2026-08-25, when a candle crashed a reader of trades. A halt must never arrive as a 0.6 sentiment score. |

## The three channels

| channel | types | who reads it | scored? |
|---|---|---|---|
| **hard facts** | `instrument-restriction`, `corporate-action`, `market-session-state` | executor, paper fill, portfolio state, risk, autonomous | never — no model, no threshold |
| **scheduled events** | `market-event` (reused) | risk, intelligence, brain | timing is fact; size is learned downstream |
| **news signal** | `news-item`, `sentiment-reading`, `news-impact-forecast` | bull, bear, tailgater, brain, scanner, hypothesis, knowledge | yes, learned |

Every level type carries `maximum_age_seconds` at its reader. A level with no
age bound is the trap this project has now paid for three times —
`part-resource-usage` holding 214 switched-off parts as running on 2026-08-26,
and `position-sizer`'s fifty-six minute stale price on 2026-08-23.

## Parts — 29 in the block

### Sources (11) — one reader per family, T-1 same shape

| part | role | produces |
|---|---|---|
| `exchange-filing-reader` | read NSE plus BSE corporate filings as they publish | `raw-news-item` |
| `corporate-action-reader` | read the exchange's own corporate action file | `corporate-action-report` |
| `results-calendar-reader` | read board meeting plus results dates before they arrive | `market-event` |
| `trading-restriction-reader` | read the exchange's F&O ban, ASM, GSM, halt lists | `instrument-restriction-report` |
| `regulator-circular-reader` | read SEBI, RBI, exchange circulars | `raw-news-item` |
| `macro-event-calendar-reader` | read the scheduled macro calendar that moves the index | `market-event` |
| `financial-press-feed-reader` | read the financial press feeds | `raw-news-item` |
| `social-chatter-reader` | read public retail chatter about a symbol | `raw-news-item` |
| `broker-news-reader` | read whatever news a broker's own API carries | `raw-news-item` |
| `web-news-searcher` | search the open web for the answer to a news question | `raw-news-item` |
| `unexplained-move-investigator` | ask why a price move has no known cause | `news-search-request` |

The last two are the autonomous half (D-N2): the investigator watches
`symbol-price-frame` against `news-item`, and when a move has no item behind it,
it *asks* — the searcher answers. Without the investigator the searcher is a
manual search box; without the searcher the investigator is a shrug.

`broker-news-reader` is declared but built last, after each broker's news
endpoint is verified against primary docs — `docs/goal.md` item 6 already flags
several broker facts as unconfirmed.

### Identify plus normalise (7)

| part | role | why it exists |
|---|---|---|
| `news-item-deduplicator` | collapse one story arriving from many outlets | keeps the **earliest** publish time — the tradable one |
| `news-text-structurer` | read one item's text into its schema-checked fields | the single LLM read, through `llm-foundation` |
| `news-symbol-resolver` | resolve the instruments a news item names | alias table built off `broker-instrument-listing`, never hardcoded |
| `news-category-classifier` | classify what kind of news an item is | routing depends on it: a halt is not an analyst note |
| `news-credibility-scorer` | score how far a source has earned trust | learned from whether the item was later confirmed by a filing |
| `news-segment-classifier` | tag which segments an item belongs to | **this is what makes "divided by segments" real** |
| `news-tape-writer` | append every news item to the tape | history accrues only in real time, same as the market tape |

Segment tagging rule: index-level macro tags `index-options` +
`index-futures`; stock-specific tags `stock-options` + `stock-futures` +
`cash-equity`; MCX tags `commodities`. An item may carry several.

The tape carries `published_at_ns` **and** `observed_at_ns` on every item. One
timestamp is not enough: replaying on publish time alone hands a backtest
information the live bot did not have until seconds later, which is exactly what
`lookahead-auditor` exists to refuse.

### Judgement — learned (5)

| part | role |
|---|---|
| `news-sentiment-model` | score which way an item points for its symbol |
| `news-surprise-scorer` | score an outcome against what was scheduled |
| `news-novelty-scorer` | score whether an item is new information |
| `news-impact-forecaster` | forecast how far the price moves on this item |
| `news-reaction-labeller` | measure what the price actually did after an item |

`news-reaction-labeller` is the part that makes the other four honest. Without
it, sentiment and impact are opinions forever; with it they are trained against
realised move, per symbol, per category. It is the news block's own learning
loop, and it mirrors `signal-outcome-labeller` in the learning loop rather than
inventing a second idea of what an outcome is.

`news-novelty-scorer` is not the deduplicator's job. The deduplicator kills the
same story from eight outlets; the novelty scorer kills the story rewritten
tomorrow, already priced.

### Hard channel — no model, no threshold (3)

| part | role |
|---|---|
| `instrument-restriction-state` | state whether an instrument may be traded right now |
| `corporate-action-adjuster` | state the price adjustment a corporate action implies |
| `market-session-calendar` | state which trading session the market is in right now |

All three are levels with an age bound. All three are read by parts that refuse
rather than weigh: `halt-enforcer`, `order-reject-classifier`,
`paper-fill-simulator`, `kline-window-builder`, `cost-basis-tracker`.

`instrument-restriction-state` merges reports from every source that carries a
restriction; the reader parses, the state part ages and merges. Splitting them
means a source going quiet expires its claims instead of leaving a name banned
forever.

### Health plus history (3)

| part | role |
|---|---|
| `news-source-health-monitor` | state whether each source is still delivering |
| `news-latency-meter` | measure how late this system sees an item |
| `news-history-reader` | serve taped news over a replay window |

Rule 8: a dead RSS feed reads exactly like a quiet news day unless something
measures it. `news-source-standing` is what `alert-raiser` fires on.

`news-latency-reading` is not only telemetry — it feeds
`news-impact-forecaster`. An item seen forty seconds late deserves less weight
than one seen in two, because the move is already gone.

## Data types — 20 new, 2 reused, 1 retired

New: `raw-news-item`, `distinct-news-item`, `structured-news-item`,
`news-symbol-tagging`, `news-category-tagging`, `news-credibility-rating`,
`news-novelty-rating`, `news-surprise-reading`, `news-item`,
`news-impact-forecast`, `news-reaction-label`, `news-latency-reading`,
`instrument-restriction-report`, `instrument-restriction`,
`corporate-action-report`, `corporate-action`, `market-session-state`,
`news-source-standing`, `news-search-request`, `historical-news-window`.

**Reused rather than re-invented:**

- `sentiment-reading` — already declared, **zero producers and zero consumers**
  since the crypto era. `news-sentiment-model` produces it. Reviving a declared
  dead type beats adding a synonym beside it.
- `market-event` — already consumed by `regime-break-detector` and
  `event-risk-limiter`. The two calendar readers produce it now, for real.

**Retired, all three crypto-era:**

| retired | why | who is re-pointed |
|---|---|---|
| `exchange-announcement-reader` (online-research) | "listings, delistings, maintenance windows" is a crypto venue | — |
| `market-event-reader` (intelligence) | only re-read `venue-announcement`; the calendars produce `market-event` directly now | — |
| type `venue-announcement` | same | `universal-symbol-sweeper` → `news-item`; `event-risk-limiter` → `news-impact-forecast` |

## Wiring — every block walked

Two new parts outside the block:

- `news-catalyst-detector` (opportunity-scanner) — raise a candidate when a news
  impact forecast clears the bar. Without it news colours a trade but never
  starts one.
- `news-history-reader` (inside the block) — serves the replayer.

| block | part | gains |
|---|---|---|
| opportunity-scanner | `universal-symbol-sweeper` | `news-impact-forecast`, `market-session-state`; `venue-announcement` → `news-item` |
| | `liquidity-grader` | `instrument-restriction` — a banned name is untradeable at any size |
| | `expiry-day-zero-to-hero-detector` | `market-session-state` — expiry-today is a fact, not arithmetic |
| | **new** `news-catalyst-detector` | `news-impact-forecast`, `news-item`, `liquidity-grade` → `entry-candidate` |
| bull-bot | `bull-feature-builder` | `sentiment-reading`, `news-impact-forecast` |
| | `bull-position-invalidation-watcher` | `news-impact-forecast` |
| bear-bot | `bear-feature-builder`, `bear-position-invalidation-watcher` | the same, short side |
| profit-tailgating-bot | `tail-mover-qualifier` | `news-impact-forecast` — news-backed or crowd-only |
| ai-brain | `intent-timing-gate` | `market-session-state`, `market-event` |
| | `premortem-writer` | `market-event` |
| | `market-thesis-reasoner` | `news-item` |
| prediction | `kline-window-builder` | `corporate-action` |
| risk-capital-allocation | `event-risk-limiter` | `news-impact-forecast`; `venue-announcement` dropped |
| | `halt-enforcer` | `instrument-restriction` |
| paper-live-trading | `order-destination-router` | `instrument-restriction`, `market-session-state` |
| | `paper-fill-simulator` | `market-session-state` |
| execution-venue-adapter | `ccxt-order-router` | `instrument-restriction`, `market-session-state` |
| | `order-reject-classifier` | `instrument-restriction` — a ban rejection is not transient |
| portfolio-state | `fill-reconciler`, `cost-basis-tracker` | `corporate-action` |
| ledger | `learning-recorder` | `news-item`, `news-impact-forecast` |
| closed-trade-decoding | `loss-cause-classifier` | `market-event`, `news-impact-forecast` |
| | `trade-narrative-writer` | `news-item` |
| learning-loop | `signal-outcome-labeller` | `news-impact-forecast` |
| hypothesis | `instruction-writer` | `news-impact-forecast` |
| | `loss-inverter` | `news-item` |
| knowledge | `symbol-profile-store` | `news-reaction-label` |
| intelligence | `market-anomaly-detector` | `news-item` — a violent print with no news is the anomaly |
| autonomous | `trading-halt-decider` | `market-session-state`, `instrument-restriction` |
| backtesting | `instruction-replayer` | `historical-news-window` |
| observability | `alert-raiser` | `news-source-standing` |
| llm-foundation | — | the block calls in: `news-text-structurer` emits `llm-request` |
| broker-adapter, market-data-feed, segment-bot, capital-desk, llm-services, resource-governor, skills, online-research | — | no consume |

## What this deliberately does not do

- **No paid news vendor.** Free official plus press plus public chatter only,
  until a measured gap says otherwise. `docs/goal.md` leaves the paid-vendor
  question open on purpose.
- **No trading decision inside the block.** It publishes facts plus forecasts.
  Whether to trade on them stays in the bots, the brain, the risk gate.
- **No per-segment copies** (D-N1). Segment separation is by tag, and the
  learning that must stay per-segment already does — each bot's feature builder
  and conviction model are per segment, and they weigh `news-impact-forecast`
  independently.
- **No news for the four Phase B segments beyond tagging.** Under
  `docs/goal.md` build order the options segments come first; commodity and
  equity sources are declared, wired, then built when Phase B starts.

## Verification

The block lands as a blueprint edit,
`dashboard/blueprint_edits/apply_2026-09-02_stock_market_news_data.py`,
idempotent, with `docs/proposals/stock-market-news-data.md` beside it. Then:

    python3 dashboard/check_contracts.py        # must print "all contracts hold"
    python3 dashboard/check_payload_reads.py
    python3 dashboard/check_part_calls.py
    python3 dashboard/build_wiring_explorer.py

The checker enforces what makes this design real: every produced type must have
a consumer (`orphan output`), every consumed type must have a producer
(`dangling input`), no `role` may contain " and " (T-6), and every part must
declare `resource_class`, `rate_risk` and `skipped_tick_effect`. A part that
nothing reads is refused by the tool, not by review.

Counts to report after the edit, measured rather than predicted: block count
28 → 29, part count 336 → 364 (29 added in the block, 1 in the scanner, 2
retired), data types 292 → 311 (20 added, 1 retired), and the derived data-edge
count before against after.
