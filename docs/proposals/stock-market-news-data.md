# Stock market news data — a foundational block, divided by segment

Given by the user 2026-09-02, designed in
`docs/superpowers/specs/2026-09-02-stock-market-news-data-design.md`,
applied by
`dashboard/blueprint_edits/apply_2026-09-02_stock_market_news_data.py`.
1 block added, 30 parts added (29 in the block, 1 in the scanner), 2 parts
retired, 20 data types added, 1 retired, 2 revived, 34 existing parts rewired.

## What was missing

The blueprint carried 28 blocks, 336 parts, 292 data types and **no news**.
Three parts gestured at it, none worked:

- `market-event` had a producer that only re-read `venue-announcement`.
- `exchange-announcement-reader` read crypto venue listings, delistings and
  maintenance windows.
- `sentiment-reading` was a declared data type with **zero producers and zero
  consumers** — dead since the crypto era.

Under the Indian goal (`docs/goal.md`) that is a hole in the substrate. Three of
the facts in it are not sentiment at all but conditions the executor cannot
trade through:

| fact | what breaks without it |
|---|---|
| F&O ban / ASM / GSM / halt | every order on the name rejects, and `order-resubmitter` retries a rejection that is not transient |
| corporate action | an unadjusted 1:1 bonus is a −50% candle. `kline-window-builder` feeds it to Kronos, every detector fires, `cost-basis-tracker` holds a basis that no longer exists |
| session state | `paper-fill-simulator` fills an overnight order at the last price it saw and journals it as a trade |

## The four decisions

| | |
|---|---|
| **D-N1** | One **global** block, output tagged by segment — not six per-segment copies. Six copies pull the same NSE feed six times and burn six times the rate limit. Joins the governor and observability as a stated exception to `segments.scope_decision`. |
| **D-N2** | Five source families: exchange/regulator official, financial press, social chatter, broker-supplied, plus an **autonomous web searcher** that goes and looks things up rather than polling a fixed list. |
| **D-N3** | The block **judges as well as delivers** (RL-060) — the LLM reads a headline once here, not six times in six segment bots, and impact is learnable only against realised price. Each bot still weighs the score its own way. |
| **D-N4** | **Three channels that never share a wire.** This repo has already paid for the alternative: `market-data` carried trades, candles and books on one wire until 2026-08-25, when a candle crashed a reader of trades. A halt must never arrive as a 0.6 sentiment score. |

## The autonomous half

`unexplained-move-investigator` watches `symbol-price-frame` against `news-item`
and, when a move has no item behind it, publishes a `news-search-request`.
`web-news-searcher` answers it off the open web.

They are two parts on purpose. Without the investigator the searcher is a manual
search box; without the searcher the investigator is a shrug. Split, the pair is
the only path in the design by which the system finds out about something nobody
pointed it at — and `news-source-standing` feeds back into the searcher so a
dead source is not asked again.

## Why `news-reaction-labeller` is the load-bearing part

Sentiment, surprise, novelty and impact are four learned parts. All four train
on one thing: what the price actually did after the item, per horizon, per
symbol, per category. Without that labeller they are opinions forever, and this
block would be exactly the "green board with nothing behind it" Rule 8 exists to
stop. It mirrors `signal-outcome-labeller` rather than inventing a second idea of
what an outcome is.

## Reused rather than re-invented

- `sentiment-reading` — revived, not duplicated. A declared type with no
  producer is a hole to fill, not a name to work around.
- `market-event` — its two existing consumers (`regime-break-detector`,
  `event-risk-limiter`) need no edit; `results-calendar-reader` and
  `macro-event-calendar-reader` now produce it for real.

## Retired

| retired | why |
|---|---|
| `exchange-announcement-reader` | crypto venue listings/delistings/maintenance |
| `market-event-reader` | only re-read `venue-announcement`; the calendars produce `market-event` directly now |
| type `venue-announcement` | crypto-shaped. `universal-symbol-sweeper` re-pointed to `news-item`; `event-risk-limiter` to `news-impact-forecast` |

## Measured after applying

    python3 dashboard/blueprint_edits/apply_2026-09-02_stock_market_news_data.py
    29 categories, 364 features, 311 data types

    python3 dashboard/check_contracts.py
    364 features, 29 categories: all contracts hold

    .venv/bin/python dashboard/check_payload_reads.py
    reads of a type nothing produces 0; reads no producer carries 0

    python3 dashboard/check_part_calls.py
    modules parsed 336, calls followed 502, calls refused 0

    blocks       28 -> 29
    parts       336 -> 364
    data types  292 -> 311
    data edges 1593 -> 1713   (health excluded)
    all edges  5278 -> 5706

Re-running the edit leaves `docs/features.json` byte-identical (md5 checked).

## What is not built

Every one of the 30 new parts is `DECLARED` — in the blueprint, contract intact,
no code. Nothing climbs to `IMPLEMENTED` until a source file named for it exists,
and the part monitor will show them dark until it does. `broker-news-reader` in
particular waits on verifying each broker's news endpoint against primary docs;
`docs/goal.md` item 6 already flags several broker facts as unconfirmed.
