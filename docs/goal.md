# Goal — ajit-segment-bots

Given by the user on 2026-09-01. This file records what was actually said. It is
the source of truth for the goal; nothing here is inferred, and additions are
made only when the user gives them.

**This supersedes the crypto goal.** The prior version of this file (a crypto
trading bot) is in git history — `git log -p docs/goal.md` — not deleted, not
carried forward as a parallel track.

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

## What that establishes

1. **Full domain conversion, not an addition.** Crypto-specific parts (venue
   adapters, symbol universes, crypto instrument types) are retired. The
   runtime substrate — governor, part harness, transistor rules T-1..T-6,
   dashboards — is reused as-is; it never knew about crypto specifically (T-4:
   a part knows nothing about the circuit).

2. **Six segments in scope, eventually:** index options, stock options, index
   futures, stock futures, commodities (MCX), and cash equity (delivery +
   intraday with margin/leverage).

3. **Build order:** intraday equity (F&O-eligible universe, ~180-200 symbols)
   and index options (nearest expiry) go first and go fully, mirroring RL-050's
   "one vertical first, fully" pattern. Stock futures, commodities, delivery
   equity, and stock options stay skeleton until their turn.

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
