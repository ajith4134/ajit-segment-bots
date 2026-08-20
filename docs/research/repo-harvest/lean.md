# QuantConnect/Lean — read 2026-08-20

Apache-2.0. C# (with parallel Python wrappers for the Algorithm.Framework
model classes). Full backtest/paper/live algo-trading engine where the same
`QCAlgorithm` code runs unchanged across all three modes — paper trading is
literally the backtest fill engine wired to a live data feed. Clone commit:
`b0006b29a26f41f48dcf5d436d15ad425f6fc9dc`.

## Mechanisms worth stealing (ranked)

1. **Iterative, fee-aware position sizing to a target buying-power percent** —
   `Common/Securities/BuyingPowerModel.cs:BuyingPowerModel.
   GetMaximumOrderQuantityForTargetBuyingPower` +
   `BuyingPowerModel.GetAmountToOrder`. Computes an initial order-size guess
   from `target_margin / margin_per_unit`, rounds to lot size in the
   direction that stays *under* target, then **loops**: build the resulting
   `MarketOrder`, price its fee, shrink `target_final_margin` by that fee,
   recompute — until the order size stops changing or overshoots, at which
   point it throws rather than silently drifting. This is the "solve for
   size given a target % of equity, net of costs" our `position-sizer`
   currently only states informally.
2. **Position-reducing orders are margin-exempt by construction** — same
   file, `BuyingPowerModel.HasSufficientBuyingPowerForOrder`: `if
   (holdings.Quantity * order.Quantity < 0 && |holdings| >= |order|) return
   Sufficient()`. A closing order never gets blocked by a margin check, no
   matter how tight the account is — prevents the exact failure mode where a
   bot can open a position but can't close it under its own risk gate.
3. **Buying power for a trade accounts for the *opposite*-side unwind cost**
   — `BuyingPowerModel.GetMarginRemaining`: when the new order flips
   direction against existing holdings, available margin = cash + (margin to
   close the existing position) + (margin to open the new one) — not just
   cash. Reversing a position is priced correctly instead of being denied
   for "insufficient margin" against a position that's about to close.
4. **`RequiredFreeBuyingPowerPercent`** — same file, used throughout: every
   margin/target calculation subtracts `totalPortfolioValue *
   RequiredFreeBuyingPowerPercent` before sizing, so the account always
   keeps a configurable cash buffer rather than sizing to exactly 100% of
   theoretical buying power.
5. **Per-security trailing stop measured from peak holdings *value*, not
   price** — `Algorithm.Framework/Risk/TrailingStopRiskManagementModel.cs:
   TrailingStopRiskManagementModel.ManageRisk`. Tracks
   `AbsoluteHoldingsValue` (price × quantity) per symbol, resets its
   high-water mark whenever the position flips side, and liquidates
   (`PortfolioTarget(symbol, 0)`) once drawdown from that peak exceeds
   `maximumDrawdownPercent` (default 5%). Using holdings value instead of
   raw price means partial fills / size changes don't reset the trail
   incorrectly.
6. **Portfolio-level drawdown breaker with two modes** —
   `Algorithm.Framework/Risk/MaximumDrawdownPercentPortfolio.cs`. `isTrailing
   =false`: drawdown measured from the run's starting equity (a hard floor).
   `isTrailing=true`: measured from the highest equity ever reached. On
   trigger it liquidates every current target and **resets its own trigger
   state** so the algorithm can resume after the breach clears — a concrete
   answer to what `drawdown-breaker` does *after* it fires.
7. **Order prices are snapped to the instrument's tick size before
   submission, per order type** —
   `Engine/TransactionHandlers/BrokerageTransactionHandler.cs:
   BrokerageTransactionHandler.RoundOrderPrices` /
   `RoundOrderPrice(decimal price, decimal increment, ...)`:
   `Math.Round(price / increment) * increment`, applied to limit price, stop
   price, trailing amount, or (for combo orders) the smallest tick size
   across every leg — with a one-time warning logged the first time a price
   actually gets rounded.
8. **Paper trading is the backtest brokerage fed live data, plus one extra
   hook** — `Brokerages/Paper/PaperBrokerage.cs` subclasses
   `Brokerages/Backtesting/BacktestingBrokerage.cs` directly; it overrides
   only `Scan()` to additionally apply dividends straight into quote-currency
   cash (`security.QuoteCurrency.AddAmount(quantity * distribution)`) before
   calling `base.Scan()`. Confirms the "paper = same fill/cost logic as
   backtest, live prices" design instead of a separate paper implementation.
9. **Fill scanning is gated by a dirty flag, and processes orders in
   deterministic ID order** — `Brokerages/Backtesting/BacktestingBrokerage.cs:
   BacktestingBrokerage.Scan`. `_needsScan` (set on submit/cancel/update,
   guarded by a lock) skips the scan entirely when nothing changed, since
   `Scan()` runs at least twice per time step; pending orders are iterated
   `OrderBySafe(x => x.Key)` so fill order is reproducible across runs.
10. **Volume-share slippage explicitly refuses to model crypto/FX/CFD** —
    `Common/Orders/Slippage/VolumeShareSlippageModel.cs:
    VolumeShareSlippageModel.GetSlippageApproximation`. Formula:
    `slippage_pct = min(order_qty / bar_volume, volume_limit)^2 *
    price_impact` (defaults `volume_limit=2.5%`, `price_impact=0.1`), applied
    as `slippage_pct * last_price`. For `Crypto`/`Forex`/`Cfd` — asset
    classes with no reliable per-bar traded volume in Lean's data model — it
    logs an error and returns **zero** slippage rather than a wrong number.
    Worth stealing the quadratic-impact formula; worth noting the model
    self-admits it doesn't apply to our asset class without real volume.

## Map onto existing parts

| existing part | repo file | what it does that ours doesn't yet say |
|---|---|---|
| `position-sizer` | `Common/Securities/BuyingPowerModel.cs:BuyingPowerModel.GetAmountToOrder` / `GetMaximumOrderQuantityForTargetBuyingPower` | iterative fee-aware convergence to a target %, with lot-size rounding biased to stay under target |
| `position-sizer` | `Common/Securities/BuyingPowerModel.cs:BuyingPowerModel.HasSufficientBuyingPowerForOrder` | closing/reducing orders are unconditionally exempt from the margin check |
| `leverage-selector` / `position-sizer` | `Common/Securities/BuyingPowerModel.cs:BuyingPowerModel.GetMarginRemaining` | reversing a position prices both the close of the old side and the open of the new side, not just free cash |
| `stop-target-placer` / `profit-lock` | `Algorithm.Framework/Risk/TrailingStopRiskManagementModel.cs` | trails on holdings *value* (price×qty) with per-symbol high-water mark reset on side flip |
| `drawdown-breaker` | `Algorithm.Framework/Risk/MaximumDrawdownPercentPortfolio.cs` | concrete fixed-floor vs trailing-high modes, and explicit re-arm after trigger |
| `order-destination-router` / `sized-order`→`order-request` | `Engine/TransactionHandlers/BrokerageTransactionHandler.cs:BrokerageTransactionHandler.RoundOrderPrices` | prices are snapped to tick size per order-type before an order-request is sent |
| `paper-fill-simulator` | `Brokerages/Paper/PaperBrokerage.cs` | concrete proof paper mode should be "backtest engine, live data" rather than a bespoke simulator |
| `execution-cost-model` | `Common/Orders/Slippage/VolumeShareSlippageModel.cs` | quadratic volume-impact slippage formula, and an explicit "does not apply without real bar volume" carve-out |

## New part proposals

- **`fee-aware-size-solver`** — block: `risk-capital-allocation`. consumes:
  `trade-intent`, `instrument-choice`, `risk-limit`, `account-balance`.
  produces: `sized-order`. An iterative refinement of `position-sizer`:
  size to a target % of equity net of the fee the sized order would actually
  incur, converging rather than sizing once and hoping fees don't push it
  over. Evidence: `GetAmountToOrder` above. Only worth splitting out if
  `position-sizer` doesn't already loop this way.
- **`order-price-tick-rounder`** — block: `execution-venue-adapter`.
  consumes: `order-request`. produces: `order-request` (snapped). Rounds
  every price field (limit, stop, trailing amount) to the instrument's
  minimum price variation before the order reaches the venue, so a rejected
  "invalid tick size" never happens for a reason the bot itself introduced.
  Evidence: `RoundOrderPrices` above.

## Anti-patterns seen

- **`BuyingPowerModel.HasSufficientBuyingPowerForOrder` special-cases option
  exercise inline** (`Common/Securities/BuyingPowerModel.cs`, ~lines
  260–296): the general buying-power check contains a branch that
  reconstructs a synthetic underlying order and recurses into itself for
  `OrderType.OptionExercise`. A part that must know about a specific
  instrument type's settlement mechanics to do its general job is exactly
  the "part that grew a second responsibility" T-6 warns about — split into
  a separate option-exercise-margin part instead of branching inside the
  general model.
- **`BacktestingBrokerage` mixes order routing, fill scanning, dividend
  application, and option-assignment processing in one 660-line class** —
  a single object owns the control state (`_needsScan`, `_pending`) *and*
  every kind of side effect that can happen to an order. Mine the
  dirty-flag/deterministic-order-scan pattern; don't copy the class shape.
- **`RoundOrderPrice` silently mutates order state as a side effect of a
  price *rounding* call** (`SendWarningOnPriceChange` fires from inside a
  function named for rounding, not for warning) — a name that hides a
  second effect, which T-7 (in our own project rules) explicitly forbids.

## Not useful here

Most of `Algorithm.Framework/Portfolio` (`BlackLittermanOptimizationPortfolioConstructionModel`,
`MeanVarianceOptimizationPortfolioConstructionModel`,
`RiskParityPortfolioConstructionModel`, the Sharpe/minimum-variance
optimizers) targets multi-asset, low-frequency equity portfolio construction
from insight *weights* across a universe — a different problem than sizing
one intraday crypto trade from a bull/bear opinion, and several assume a
covariance matrix estimated over daily bars that doesn't exist at intraday
crypto cadence. `MaximumSectorExposureRiskManagementModel` assumes GICS
sector data that has no crypto equivalent. The equities/futures/options
machinery throughout `Common/Securities` (`Cfd/`, `Equity/`, `Forex/`,
`Future/`, `FutureOption/`, the `Cash`/`CashBook` multi-currency settlement
system) is out of scope for a crypto-only bot. Skipped: `Common/Securities`
subfolders beyond the top-level margin/buying-power files (per-asset-class
implementations), `Engine/TransactionHandlers/OrderRequestProcessingPool.cs`
and `Engine/TransactionHandlers/CancelPendingOrders.cs` (queueing plumbing,
not a trading mechanism), and all of `Algorithm.Framework/Portfolio`'s
optimizer math beyond noting it targets a different portfolio-construction
problem than ours.
