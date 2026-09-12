# broker-order-router: the part that can spend real money on NSE

**2026-09-12.** Asked for by the operator: *"build the upstox order router"*. It
is the last of the four `parts/` files still naming a crypto venue — the
Indian replacement for `ccxt-order-router`, which is the only thing standing
between this project and a live NSE order.

## What already existed, and what did not

`UpstoxAdapter` has had the whole broker side since the cutover:
`order_endpoint_url()` (Upstox's separate low-latency host),
`build_order_request_payload()` (including Upstox's own `instrument_token`
misnaming, preserved rather than "fixed"), `read_order_result()`, and an
`OrderRequest` in the fields Upstox's place-order API actually takes.

**No part drove any of it.** The adapter could place an order and nothing ever
called it, so the entire live path on this market was unreachable — which is
also why nobody noticed it was missing: the paper book filled everything and
reported healthy.

## The shape

Same contract as `ccxt-order-router`, so nothing downstream changes:

| | |
|---|---|
| consumes | `order-request`, `broker-token-standing`, `money-mode`, `symbol-universe`, `cancel-decision`, `order-reprice` |
| produces | `raw-venue-order-status`, `part-health` |

`raw-venue-order-status` is deliberately the **same data type** the crypto
router produces, not a broker-specific one. `venue-order-status-translator`,
`order-reject-classifier`, `order-not-found-debouncer` and `clock-skew-monitor`
already read it, and a new type would mean four more parts or a bridge. T-6:
grow by adding a part, not by making the neighbours cleverer.

`symbol-universe` is needed because Upstox places orders by `instrument_key`
(`NSE_FO|51420`) while every other part names the instrument by its trading
symbol (`NIFTY 24550 CE 08 SEP 26`). That mapping already rides on the universe,
and `broker-margin-quoter` reads it the same way for the same reason.

## Five gates, and why there are five

This is the part that spends money, so it is built the way the crypto router's
own docstring says such a part must be — and then two gates further, because
this one talks to a real broker holding real rupees.

1. **Not destined for the live venue → refused.** A paper order belongs to
   `paper-fill-simulator`. This mirrors that part's own first refusal, pointing
   the other way, so each book refuses what the other owns rather than both
   trying.
2. **The segment's money mode is not exactly `live` → refused.** A second,
   independent gate reading a different producer. `destination` is decided by
   `order-destination-router`; `money-mode` is read from the segment's own
   settings file by `money-mode-reader`. **One flag is one bug away from
   spending real money**, and these two would have to be wrong together.
3. **No valid token → refused.** Age-bounded, the same
   `broker_token_standing_maximum_age` the margin quoter uses. A token that
   stopped being restated is not a token.
4. **No instrument key for the symbol → refused.** Sending Upstox a trading
   symbol where it expects an instrument token is not one rejected order: it is
   an order for whatever that string happens to resolve to, or nothing.
5. **An id already sent → refused as a duplicate.** The client order id is a
   hash of the order's own content plus its intent id, so a retry of one
   decision is the same id and a second genuine decision asking for an identical
   order is a different one. Remembered locally and sent as Upstox's `tag`.

Every refusal is published as a `raw-venue-order-status` with its own named
outcome rather than dropped. An order that vanishes silently is the state this
project has been bitten by repeatedly — a gate, a router and a book each
reporting nothing wrong while no order exists.

## Numbers, not literals

`broker_order_product`, `broker_order_validity` and `broker_order_timeout_seconds`
are settings with provenance (RL-061). Product is `D` for the two options
segments — a bought option is held to its own expiry, not squared off intraday —
and stating it rather than hardcoding is what lets a future intraday segment say
`I` without an edit here.

## What this deliberately does NOT do

- ~~It places orders. It does not cancel or reprice them.~~ **Both landed the
  same day**, once Upstox's own v3 docs were read rather than guessed at:
  `DELETE /v3/order/cancel?order_id=...` (query parameter, no body) and
  `PUT /v3/order/modify` (JSON body, with `order_type`, `validity`, `price` and
  `trigger_price` all required even when unchanged — the API assumes the
  original order only for fields left out entirely). Both take the broker's
  order id while both decisions name the client's, so the router keeps the
  mapping it learned from its own place response; it is the only part that ever
  holds both.
- **It does not poll for status.** `order-state-poller` is that part and is off
  the spine with the rest of the crypto execution cluster.
- **It is not on the live spine.** It is declared, built and tested, and
  `operate/run_live_spine.py` does not start it. Both segments are on paper, so
  a router that could reach Upstox's place-order endpoint has nothing legitimate
  to do — and a part that can spend money should be started deliberately, on the
  day the operator moves a segment to live, not inherited from a commit.

## Verification

- Refusal on every one of the five gates, each asserted separately, including
  the two independent live-money gates tested one at a time so neither can be
  the only thing standing.
- The payload Upstox would actually receive, checked field by field against
  `UpstoxAdapter.build_order_request_payload` with a real instrument key and lot
  size from the instrument master (RL-063).
- No HTTP in any test: the transport is injected, because a test that can reach
  `api-hft.upstox.com` is a test that can place an order.
