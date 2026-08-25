# live-switch-guard reads the record it judges

**Proposed by Claude, 2026-08-25, during the payload-shape sweep.**

## What was found

The guard decides whether real money may be traded. It tests four things at once
(RL-005): enough closed trades, a positive result after costs, a drawdown inside
tolerance, and enough elapsed time. It assembled all four out of `bot-maturity`:

    closed_trades=int(getattr(maturity, "trades_here", getattr(maturity, "closed_trades", 0))),
    net_result_after_costs=float(getattr(maturity, "net_result_after_costs", 0.0)),
    worst_drawdown_fraction=float(getattr(maturity, "worst_drawdown_fraction", 1.0)),
    days_traded=float(getattr(maturity, "days_traded", 0.0)),

`BotMaturity`, the type `edge-graduation-gate` publishes on that wire, carries
`bot`, `regime`, `state`, `is_mature`, `trades_here`, `required_trades`,
`conditions_met`, `failing_conditions`, `reason`, `judged_at_ns`. Three of the
four numbers above have never existed on it, so three of the four tests have
always been judged against their `getattr` defaults: a net result of 0.0, a
drawdown of 1.0, and 0.0 days traded.

The guard refuses everything, which is the safe direction and the reason this was
never noticed. **It refuses for reasons that are not measurements**, and the day
someone widens the tolerances to get past it, it will still be refusing on
defaults -- or, if the defaults are ever made permissive, permitting on them.

## Why this needs a blueprint edit

The numbers exist; the guard has no wire to any of them.

| test | where the measurement actually lives |
|---|---|
| enough closed trades, per bot | `bot-scorecard` -- `bot-scorekeeper` attributes outcomes to the bot that gave the opinion |
| positive after costs | `closed-trade` -- `realised_pnl` and `fees_paid`, and net is the difference |
| drawdown inside tolerance | `drawdown-episode` -- `depth_fraction` from the equity peak |
| enough elapsed time | `closed-trade` -- the span from the earliest `opened_at_ns` seen to now |

    live-switch-guard  consumes += bot-scorecard, closed-trade, drawdown-episode

## The fifth test, which is what bot-maturity was for

`bot-maturity` stays, doing what it actually says: `is_mature` is
`edge-graduation-gate`'s verdict that a bot's edge has graduated **in a named
regime**. A bot whose edge is mature nowhere has not earned real money, and that
is a different question from its account record. Five tests, each from the part
that measures it.

## What this deliberately does not claim

- **Three of the tests are account-level, not per-bot.** Net result, drawdown and
  elapsed time are measured across the segment, because a closed trade carries no
  bot attribution and inventing one here would be worse than saying so. Each
  verdict states it.
- **Elapsed time is measured from the earliest trade this part has seen**, not
  from the bot's first ever trade: the guard holds this in memory and a restart
  starts the clock again. That delays graduation and never hastens it, which is
  the direction a guard should fail in.
