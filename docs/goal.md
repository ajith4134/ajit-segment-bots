# Goal — ajit-segment-bots

Given by the user on 2026-09-01. This file records what was actually said. It is
the source of truth for the goal; nothing here is inferred, and additions are
made only when the user gives them.

**This supersedes the crypto goal.** The prior version of this file (a crypto
trading bot) is in git history — `git log -p docs/goal.md` — not deleted, not
carried forward as a parallel track.

## TEMPORARY GOAL — given 2026-09-05 (second), read this first

The user asked that this be read at the start of **every** session, so that
reminding is not theirs to do, and asked explicitly that it **stay active
across every session until it is achieved completely** — it is not a
one-session task and is not done until every part of it below is.

> "I need you to create and save the new temporary goal on checking all the 29
> foundational features one by one including all its internal parts to see if
> the data is flowing to its connection output and input and find all the
> parts that still are or works on crypto and after finding convet or replace
> it with it's similar version for the Indian stocks and find if we need any
> extra foundational features aside 29 and in any foundational feature If need
> new or more parts"
>
> "And make this temporary goal to be active across all the sessions until we
> achieve the goal completely"

**A systematic audit of all 29 foundational features (`docs/features.json`
categories), one at a time, each covering every part inside it:**

1. **Data-flow verification, per part, both directions.** For every part in the
   category: is data actually arriving on each `consumes` type, and is data
   actually leaving on each `produces` type — measured, not inferred from the
   contract declaration. `dashboard/check_contracts.py` already proves the
   wiring is *declared* correctly (R-01); this goal proves messages actually
   *move* across each wire, the same distinction RL-072 draws between "wired
   up" and "carrying."
2. **Find every part still crypto-shaped.** Anything still reasoning in
   crypto's vocabulary or against crypto's venues (funding rate, USDT
   notional, a Binance/Bybit adapter call, a crypto-only data type) rather
   than the Indian-market equivalent, in a part that is supposed to be live
   for this project's actual goal.
3. **Convert or replace, never leave in place.** Each crypto-shaped part found
   gets converted to its Indian-stock analogue or replaced by one, the same
   standing instruction the crypto retirement has followed throughout: replace
   with the real Indian-market equivalent, never just delete and leave a gap.
4. **Whether 29 is enough.** Once every existing foundational feature has been
   walked, name any foundational feature this project needs that does not
   exist yet among the 29.
5. **Whether each existing foundational feature is complete.** For every one
   of the 29, name any part it is missing to do its job fully — not just
   "does it run," but "does it have every part its own role needs."

This is a large, multi-session undertaking by its own description (29
categories, each walked in full) — it is not expected to finish in one
sitting, which is exactly why the user asked for it to persist across
sessions rather than be re-explained each time.

**The ledger is `docs/feature-audit.md`** — which feature has been walked, what
was measured in it, what was found and fixed, and what it is still missing.
Read it at the start of a session before walking anything, and append to it
rather than rewriting it; a goal that spans sessions needs a record that spans
sessions or each session re-walks what the last one already did. The measuring
instrument is `python3 dashboard/audit_feature_dataflow.py`, which reads each
part's own bus counters and keeps "nothing has travelled on this wire" apart
from "nobody has looked at this wire".

## TEMPORARY GOAL — given 2026-09-05 (first), still standing underneath the one above

The user asked that this be read at the start of **every** session, so that
reminding is not theirs to do. It sits on top of the standing goal below; it
does not replace it, and the goal above does not replace this one either —
both are active until each is achieved.

> "is te option index sement bot is completed fully and ready to start
> pappertradin if so can me move on to build te option stock sement bot and
> after tat full univers cas equity intrady wit 5x seent bot make tis te
> temperary [goal] all tis 3 bots to be ready by monday to start pappertradin
> on live market data and wit all parts connection wit consistensis wit only
> creatin seperate partes wen tey are nessery"

**Three segment bots, paper trading on live market data by Monday 2026-09-07**
(NSE opens 09:15 IST / 03:45 UTC):

1. **index options** — built, and as of 2026-09-05 it has never completed a
   paper trade. Not proven; Monday's open is its first real test.
2. **stock options**
3. **cash equity intraday, full universe, 5x leverage**

Every part connected and consistent, and **new parts created only where they are
genuinely necessary** — the existing segment-generic parts are reused rather than
cloned per segment.

**Progress, 2026-09-05:** all three segments verified opening AND closing a
trade together on the real captured tape (`operate/replay_a_captured_session.py`,
2026-09-04), not inferred — `index-options 500 tried/343 opened/216 closed,
stock-options 254/80/13, cash-equity-intraday 50/49/23, "Segments that opened
AND closed a trade: 3 of 3"`. Cash-equity's 50 there is a real top-50 shortlist
built from live Upstox 52-week/ATR history plus the tape's own real momentum
and gap for that day (`equity-opportunity-profiler`,
`cash-equity-shortlist-ranker`), not the full unranked universe and not the
old static 14-name list. Fixed the same day: `segment_underlying_trading_symbols`
was disconnected from the derived 2,444-share universe, so only 14 names could
ever be attributed to cash-equity's capital regardless of what the scanner saw
— `segment_that_trades` and `instrument-selector`'s
`segment_resolver_from_settings` now resolve a derived-selection segment's
ownership against the live shortlist instead. **This is still replay, not
live paper trading** (RL-071) — Monday's open on real live data is still the
first real test this goal names.

**Two conflicts with item 3 below, flagged rather than silently resolved:**

- Item 3 (the user's own correction of 2026-09-01) puts cash equity intraday
  with margin in **Phase B**, after both options bots are complete end-to-end.
  This temporary goal pulls it forward.
- **Resolved before this session (verified 2026-09-05, this note was stale):**
  the 5x-leverage equity bot needed machinery this project had parked as "not
  a gap — does not apply to buy-only options". `leverage-selector` is no
  longer one of them: commit `b0a2d37` turned it on, sized against the
  broker's own `broker-margin-requirement` carry cost (replacing a crypto
  `funding-forecast` placeholder that had been silently halving every Indian
  leverage choice), and `position-sizer` reads its `leverage-choice`
  (confirmed by reading both files, 263 tests pass). `liquidation-price-
  tracker` and `paper-liquidation-simulator` stay off, and that is now a
  documented finding rather than an omission (`operate/run_live_spine.py`,
  same commit): an intraday equity position on Indian broker margin is not
  liquidated at a price the way a perpetual is — the broker squares it off
  from around 15:15 IST, which `intraday-square-off-placer` already models,
  live on the spine. A liquidation price from a maintenance-margin rate would
  be a number this market does not quote; those two stay declared-but-off
  until a real margin-shortfall model is built, which is real unbuilt work
  and not a switch.

## The goal in the user's words

> Convert this project into a bot trading Indian stock market — segments: index
> options, stock options, index futures, stock futures, commodities, and
> delivery-or-intraday equity using leverage/margin buying. All parts work
> toward Indian trading. Start with intraday stocks (full universe, F&O-eligible
> subset first) and index options at nearest expiry. Combine all the trading
> brokers — Upstox, Angel One, Fyers, ICICI Direct, Zerodha, Groww — use all to
> their fullest extent.

Clarified in conversation: "fullest extent" means **redundancy and rate-limit
headroom**, not parallel capital across accounts — one broker executes live
orders, the rest cover data redundancy and split API load. Paper trading before
any live order, same standing rule as the crypto build.

**Build order corrected 2026-09-01, the user's own words:**

> In goal: first build the options index and options stocks — 2 segment bots
> — complete, with paper trading on historic data, and when market opens on
> real live data with all the 332 and more parts working and trading on this
> new segment bots. Only after [that] we move to the other segment bots like
> the stocks intraday with margin bot, and futures and commodity. When we
> complete them end to end, then our goal is complete.

(Lightly cleaned up from the original for readability; nothing added or
removed in meaning.) See item 3 below for what this establishes.

## What that establishes

1. **Full domain conversion, not an addition.** Crypto-specific parts (venue
   adapters, symbol universes, crypto instrument types) are retired. The
   runtime substrate — governor, part harness, transistor rules T-1..T-6,
   dashboards — is reused as-is; it never knew about crypto specifically (T-4:
   a part knows nothing about the circuit).

2. **Six segments in scope, eventually:** index options, stock options, index
   futures, stock futures, commodities (MCX), and cash equity (delivery +
   intraday with margin/leverage).

3. **Build order — corrected 2026-09-01.** Options first, both of them,
   fully, before anything else moves:

   - **Phase A: index options and stock options — two segment bots, each
     complete end-to-end.** Paper trading on historic data first; when the
     market is open, paper trading continues on real live data, with all
     332+ parts genuinely running and trading on these two segment bots —
     not skeleton, not a subset. Phase A is not done until both are live-data
     paper-trading for real, fully wired.
   - **Phase B, only after Phase A is fully complete:** the remaining
     segment bots — intraday equity (with the margin/leverage bot), stock
     futures, index futures, commodities. Same discipline: each one built
     completely end-to-end before the next starts, not several in parallel
     half-done.
   - **The goal is complete when every segment bot from both phases is
     built end-to-end** — Phase A's two plus Phase B's remaining ones.

   This replaces the earlier reading of "start with intraday stocks and
   index options" as *both* going first together — the user's correction
   makes options-only Phase A explicit, with intraday equity (and its
   margin bot), futures and commodities moved to Phase B. Mirrors RL-050's
   "one vertical first, fully" pattern, now applied at the two-segment-bots
   granularity rather than the whole six at once.

4. **Full NSE cash universe (~2000+ symbols) is a planned upgrade, not in scope
   now.** Tracked in `docs/future-upgrades.md` so it isn't lost or silently
   implied into the first build.

5. **Multi-broker by design, all six wired in as swappable adapters (T-1).**
   No single broker is privileged in the code — the same adapter contract
   every broker satisfies is what lets six of them coexist without any one
   feature depending on which broker answered. Roles:
   - **One broker executes live orders** (the primary).
   - **The rest feed redundant market data and absorb API load** — spreading
     the symbol universe's polling across six accounts' rate limits, and
     giving the primary a fallback if its feed or API goes down.
   - **No cross-account capital pooling.** Positions are not split or scaled
     across brokers; the primary's account is the capital account.

6. **Build order for the six brokers**, researched 2026-09-01 (see
   `docs/research/` or the session — free tier, sandbox support, rate limits,
   historical data depth, SEBI documentation compared across all six):

   | Order | Broker | Why here |
   |---|---|---|
   | 1 (primary, first built) | **Upstox** | Only one of the six with a genuinely documented, always-on sandbox — free, historical data to 2005, all segments covered. Paper trading fits this directly. |
   | 2 | Zerodha (Kite Connect) | Best-documented SEBI algo-ID/static-IP compliance path; ₹500/mo for data, orders free. No sandbox — self-simulated paper trading against its live feed. |
   | 3 | Angel One (SmartAPI) | Free, explicit SEBI static-IP documentation. |
   | 4 | Fyers | Free, deepest confirmed historical depth (25yr daily / 5yr intraday) — useful once backtesting matters. |
   | 5 | ICICI Direct (Breeze) | Free but tightest rate limits (100 calls/min, 5000/day); lowest priority of the free tier. |
   | 6 | Groww | Newest, paid (₹499-2000/mo), only 3 months of historical data, MCX support unconfirmed against official docs — last. |

   Upstox is built and proven in paper mode before a second adapter is added —
   same "one vertical first, fully" discipline as the segment build order.

7. **Paper before live**, same standing rule as the crypto build (RL-005
   precedent) — full experimentation on live Indian market prices, no real
   order until the strategy is proven. Moving to live capital is what makes
   SEBI's algo-ID empanelment (below) a blocking requirement rather than a
   future concern.

## Not yet decided (yours to answer, not a deferred default)

- Historical/backtest data source depth beyond what each broker's own API
  provides — whether a paid vendor (Truedata, Global Datafeeds) is ever
  needed, or broker-native history is enough.
- SEBI's Feb 2025 algo-trading circular requires an exchange-approved algo ID
  and static-IP whitelisting for **live** API order placement. Doesn't block
  paper trading. Zerodha, Angel One, and Fyers document this process
  explicitly; Upstox implies a registration tier; ICICI's process wasn't
  found in this pass. This becomes a real registration step before Upstox (or
  whichever broker is live by then) ever places a real order.
- Commodities (MCX) segment: separate exchange, likely separate data
  agreement — stays fully skeleton until the equity+options vertical is
  proven, same as spot/options did for crypto.
- Exact numeric details several sources disagreed on or couldn't confirm from
  primary docs (Zerodha's daily order cap, Angel One/Fyers rate limits and
  WebSocket caps, Groww's official MCX coverage) — worth re-verifying against
  primary docs directly before the adapter for that broker is actually built,
  not relied on from this research pass alone.
