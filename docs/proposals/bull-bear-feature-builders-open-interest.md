# bull-feature-builder / bear-feature-builder: open interest replaces funding

Given by the user 2026-09-01, applying the standing crypto-retirement
guidance: when a crypto-only feature is deleted, replace it with the honest
Indian-market analogue built from real broker data, don't just strip the
input and shrink the model's view.

`funding_rate` and `funding_forecast_change` were a proxy for crowd
positioning pressure. `broker-open-interest` (already built,
market-data-feed's broker-adapter work) carries the same kind of signal for
Indian derivatives: how much an underlying's option-chain open interest is
building, and which side (buy or sell) is driving today's volume.
`runtime/underlying_open_interest.py` resolves per-contract open interest to
its underlying via `broker-instrument-listing` and sums the chain, since
open interest is published per option contract, not per underlying.

**bull-feature-builder:** `funding_rate`/`funding_forecast_change` ->
`open_interest_change` (fractional change over the builder's long window,
reusing the existing `_return_over` machinery) and `order_flow_imbalance`
(buy quantity less sell quantity, over their total).

**bear-feature-builder:** the same two features, plus `sell_flow_imbalance`
-- signed for the short the same way `offer_side_imbalance` already is,
positive when sell-side flow is heavier. `funding_carry_over_horizon` (the
actual cost of holding a short across settlements) is retired **without** a
replacement: that needs an index/stock futures basis, which does not exist
yet (futures segments are Phase B, per `docs/goal.md`). Twelve features
survive where thirteen did; twelve real ones, not thirteen where one is
guessed.

Both parts' `consumes` moves from `funding-forecast` to
`broker-instrument-listing` + `broker-open-interest`. `symbol-universe`,
`symbol-price-frame`, `order-book-snapshot` and `symbol-profile` are
unchanged -- still crypto-shaped, out of scope for this slice (see
`docs/proposals/broker-underlying-price-frame-bridge.md` for the one already
converted, `symbol-price-frame`).
