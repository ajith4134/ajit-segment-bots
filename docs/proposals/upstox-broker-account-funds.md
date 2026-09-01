# Broker account funds reader

Given by the user 2026-09-01 ("start the next phase of the plan" →
"order placement and margin for Upstox"), designed in
`docs/superpowers/specs/2026-09-01-upstox-adapter-design.md` §6/§6a/§9b,
applied by `dashboard/blueprint_edits/apply_2026-09-01_upstox_account_funds.py`.
1 part, 1 data type added to the existing `broker-adapter` block.

## Why this part and not order-router/margin-reader

Asked for "order placement and margin." Investigation found the two split
cleanly on one question: does answering it need an order?

- **Account funds** (`GET /v2/user/get-funds-and-margin`) needs only a
  valid token. Direct analogue of `venue-balance-reader` — `key-standing`
  in, `account-balance` out, no order dependency there either.
- **Order placement** and **per-order margin** (`POST /v2/order/place`,
  `POST /v2/charges/margin`) both need an order intent — instrument,
  quantity, side, product — as input. Nothing in this project produces one
  for Indian markets. The crypto equivalent (`order-request`) comes from
  `order-destination-router`, deep in a fully-wired opportunity-scanner →
  sizing → risk-gate pipeline with no Indian counterpart yet.

Reusing crypto's `order-request` type was rejected for the same reason
`market-data`/`candle`/`order-book-snapshot` were (spec §5,
`docs/proposals/upstox-broker-adapter.md`): it would auto-wire this
segment into `order-destination-router`'s crypto-shaped orders via R-01. A
new `broker-order-request` type has no producer at all yet, which the
contract checker refuses outright as a dangling input — not a formality,
a real check that caught exactly the gap this investigation found.

So this pass declares only what has a real, self-contained answer.
Order-placement and margin-quote capability still gets built this phase —
as tested methods on `UpstoxAdapter`, not a blueprint part with nothing
upstream to call it. The part gets declared the day an Indian
opportunity-scanner/sizing pipeline exists to produce `broker-order-request`.

## The part

| Part | Reads | Writes | Does |
|---|---|---|---|
| `broker-account-funds-reader` | `broker-token-standing` | `broker-account-funds` | polls Upstox's funds endpoint, refuses a reading past its freshness bound rather than serving a stale one as current (same discipline as `venue-balance-reader`) |
