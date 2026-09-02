# Index options: a paper trade opened and closed, on history when the market is shut

**The win condition, in the user's words (2026-09-02):** place paper trades on
historic data; when those paper trades are placed *and closed* using historic
data, we call it a win. Clarified in the same session:

> no when market is closed only then historic data, but if the market is open
> then on the live data paper trades

So the price source is chosen by the market session, and nothing else changes:
the same 341 parts, the same decisions, the same fill path. This is Phase A of
`docs/goal.md` — index options first, complete — not a new direction.

**Why this does not contradict RL-071.** That ruling says a tape replay is never
what a *live run's* decision or learning is made from. It is not violated by a
replay that runs only when there is no live market to read: the rule exists so
that a bot which could be trading real prices is never fed recorded ones, and
market-session-state is exactly the fact that tells the two apart. What is
forbidden and stays forbidden: replaying history *while the market is open*, or
letting a replayed fill be recorded as a live one.

---

## What was measured before planning, 2026-09-02

Every number here was taken on this machine today, not estimated.

| | |
|---|---|
| Parts in the blueprint | 366 |
| Parts with a module and a `start_part` | **341** |
| Parts with no module at all | **25** — 24 in `stock-market-news-data`, plus `news-catalyst-detector` |
| Parts reporting on the live spine | **313 of 313**, 0 silent |
| Files still naming crypto (`binance`/`bybit`/`usdt`/`perpetual`) | 96 |
| `segment_id` | already `index-options` |

**Upstox historical candles work, and were proven against the live API today:**

    GET https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}

    ["2026-09-01T15:39:00+05:30", 376.45, 376.45, 375.0, 375.95, 3835, 200395]
     timestamp                    open    high    low    close   vol   open_interest

- Units `minutes|hours|days|weeks|months`. Minute data from **January 2022**,
  one month per request for 1-15 minute intervals.
- The row carries **open interest**, which the live feed also carries — so a
  replayed bar can feed the same consumers a live bar does.
- Newest-first, and stamped **+05:30**. Both matter: the reader must sort, and
  must never read the stamp as UTC (04:45 UTC is 10:15 IST — read as UTC the
  market would be shut for its entire morning, the same trap
  `market-session-calendar` already documents).

**Expired contracts are absent from the instrument master** — the master holds
only live contracts, earliest expiry 2026-09-08. So history is fetched for
*currently listed* nearest-expiry contracts, which have already traded on prior
days. That is enough for the win condition and avoids needing an expired-contract
data source at all.

---

## The one thing that blocks the win today

`paper-fill-simulator` refuses to fill while the market is closed — added this
morning (commit `3f73d4d`), because it had been filling overnight orders at the
15:29 price and journalling them as trades. **That guard would refuse every
historic fill**, since replay runs precisely when the market is shut.

The fix is not to weaken the guard. A replayed bar carries its own timestamp,
and the session that matters for it is the session **that bar was printed in** —
15:39 IST on 2026-09-01 is inside a real trading session. So the simulator must
judge the session of the price it is filling against, not of the wall clock.
That keeps the original defect fixed (an overnight order still cannot fill at a
stale price) while letting history fill at the time it really happened.

This is the shape the whole plan turns on: **a replayed bar is a fact about a
past moment, and every part that asks "what time is it" must ask the bar.**

---

## Tasks

Each is one commit, TDD, tests written failing first (RL-063: against real
captured data, which for this plan means real Upstox history).

- [ ] **1. `read_historical_candles` on the broker adapter.** URL builder plus a
      row reader returning the project's own `BrokerCandle`, with the IST stamp
      converted once, at the edge. Implemented for Upstox; it is the only broker
      built. Adding an abstract method to `BrokerAdapter` is what crash-looped
      every part on 2026-08-30 when it was implemented for one venue only — so
      it is added and implemented in the same commit, and the spine is restarted
      and its journal read before the commit is called done.

- [ ] **2. `broker-history-reader`** — a part that fetches history for the
      instruments the selector is tracking and publishes them as candles, paced
      so the month-per-request window and the rate limits are respected. Blueprint
      edit first (`dashboard/blueprint_edits/`), then the code.

- [ ] **3. The session a bar belongs to.** `paper-fill-simulator` (and every
      part that gates on `market-session-state`) judges the bar's own moment.
      The existing "no fill while the market is closed" test must keep passing
      unchanged — it is the regression this must not undo.

- [ ] **4. Replay drives the chain when the market is shut.** The historic bars
      go onto the same wires the live feed uses, so the bull bot, the arbiter,
      the sizer and the fill path see no difference. No part learns it is being
      replayed (T-4).

- [ ] **5. Open and close one paper trade on history, and prove it.** A
      measurement under `measurements/`, reading the position journal and the
      closed-trade record — not a log line, the same journal the trade board
      reads. This is the win condition and it is measured, never asserted.

- [ ] **6. Then, and only then, the breadth work:** the 25 parts with no module,
      and the 96 files still naming crypto. Deliberately last — a part that has
      never run cannot be shown to work on Indian data, and the vertical proves
      the substrate before the breadth widens it (RL-050's pattern).

---

## What is deliberately not in this plan

- **Stock options.** Phase A is both index and stock options, and this plan is
  the index half. The second is a repeat of a proven vertical, not a new design.
- **A second broker.** Upstox is proven in paper mode before an adapter is added
  (goal.md item 6).
- **The 24 news parts.** They are `DECLARED` and honestly empty. The status board
  paints their block red and should keep painting it red until they are built.
