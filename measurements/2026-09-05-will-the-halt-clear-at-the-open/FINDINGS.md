# Will the halt clear at Monday's open? — 2026-09-05

The question was whether bot 1 (index options) can trade at all on Monday. On
the shut-market spine `trading-halt-decider` reported **211,399 of 215,357
decisions halted** for `the-market-data-does-not-make-sense`, which looked like
the single blocker standing between this segment and its first paper fill.

**It is not a blocker.** Three measurements, in the order they were made.

## 1. The first measurement was invalid, and said so

Anchoring the silence rule to NSE's session clock reported 100% of symbols
halted at 09:20 and 10:15 on 2026-09-04 — every one of them "never printed
yet" — while 6,665 symbols demonstrably printed later that day.

The cause is not the rule. On both recorded sessions the feed's own first print
came hours after the open:

    2026-09-02   first print 14:07 IST   (open 09:15)
    2026-09-04   first print 10:33 IST

Both sessions ran while `broker-instrument-catalogue-reader` was still
publishing the instrument master once an hour into a socket buffer that holds
532 rows. `a8bdbb0` fixed that at 21:41 UTC on 2026-09-04 — **after** both
sessions. So the session-clock reading measured that outage and called it a
silent symbol. The checkpoints are anchored to the feed's own first print now.

## 2. Pooled, the rule halts ~88% of symbols — and that is mostly correct

Judged four hours into a live feed on 2026-09-04, 5,776 of 6,665 symbols are
called silent. Pooled, that number is meaningless: it is dominated by
instruments that genuinely do not print for five minutes at a time. Measured
per exchange prefix instead, which is the standing lesson here:

    prefix        trading   HALTED    never   halted share
    NSE_FO            786     2083     2093    84.2%
    NSE_COM             2     1314     1466    99.9%
    NCD_FO              2      444      834    99.8%
    NSE_EQ             45      486      583    96.0%
    BSE_FO             51       37      387    89.3%
    NSE_INDEX           2        5        3    80.0%

## 3. The instruments this segment actually trades are clean

    NSE_INDEX|Nifty 50            trading   prints 14,248  quiet 0s  median gap 0.3s
    NSE_INDEX|Nifty Bank          trading   prints 12,042  quiet 0s  median gap 0.3s
    BSE_INDEX|SENSEX              trading   prints  3,011  quiet 1s  median gap 1.0s

    NSE_INDEX|Nifty Rural         HALTED    prints     35  quiet 8,135s
    NSE_INDEX|NIFTY100 ESG        HALTED    prints    179  quiet 7,923s
    NSE_INDEX|Nifty Consumption   HALTED    prints  4,848  quiet   404s
    NSE_INDEX|Nifty FPI 150       HALTED    prints  2,268  quiet   404s
    NSE_INDEX|NIFTY100 Qualty30   HALTED    prints  3,109  quiet 11,407s

Every halted index is one this segment does not trade.

## 4. The halt is symbol-scoped, and the scope is honoured

Checked in the code rather than assumed, because a scope nothing reads is the
defect this project keeps finding:

- `trading_halt_decider.decide` — `symbol_only = set(causes) <= {REGIME_BROKE,
  MARKET_ANOMALY}`, and when that holds the scope becomes the affected symbols
  rather than `EVERYTHING`.
- `halt_enforcer.read_limit` — parses that scope through `symbols_in_scope` and
  puts it on the `RiskLimit`. Its own docstring records the defect already fixed
  on 2026-08-25: "two anomalous symbols out of a hundred stopped every trade the
  system could make, and the only trace was a counter of zero limits issued."

So an anomaly on Nifty Rural does not stop a NIFTY trade.

## What this leaves as the real blocker

The fill path is proven on real captured data. `operate/replay_a_captured_session.py`
on 2026-09-04's tape opened 12 contracts and closed all 12 through the same
parts the spine runs, with Upstox's real charge stack on both legs:

    opened 12   closed 12   fees 694.07   net -86.57   4 of 12 in profit

What has **never** completed is the path from a detector's candidate to an
intent that reaches that fill path — and it cannot be tested off a replay,
because `broker-history-reader` produces `market-data` and `candle` but not
`symbol-price-frame`, so a replay reaches the fill path and the candle path and
never the decision path (RL-071, recorded 2026-09-04).

**Monday's open is the first time that middle can be observed at all.** The
things to watch, in order: `opinion-arbiter` producing anything, then
`instrument-selector`'s refusal counters — `refused_stop_invalid` was 84% of
actionable intents on 2026-09-04 before the paise/rupees tick fix, and that fix
has never run against a live market.
