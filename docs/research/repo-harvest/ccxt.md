# ccxt/ccxt — read 2026-08-20

MIT licence (Copyright 2024 Igor Kroitor). Python (the repo also generates JS/PHP/C#/Go from the
same source, not read here). Unified REST/WS trading API across 100+ crypto exchanges. Commit
`8c7ed512d3f5eff0d4e08b9c424d18f863b98650`.

## Mechanisms worth stealing (ranked)

1. **Leaky-bucket rate limiter, one bucket per exchange instance.**
   `python/ccxt/base/exchange.py:Exchange.init_rest_rate_limiter` builds a token bucket with
   `capacity: 1`, `delay: 0.001` (ms), `refillRate: 1/self.rateLimit`. `Exchange.throttle` (line 449)
   is the sync fallback: it tracks `lastRestRequestTimestamp`, computes `sleep_time = rateLimit * cost`,
   and sleeps the difference if the elapsed time is shorter. `rateLimit` is milliseconds per unit-cost
   request (binance.py sets it to `50` — i.e. 20 requests/sec base unit) and every endpoint declares
   its own `cost` multiplier in its `describe()` (e.g. `depth` costs 2-20 units depending on `limit`,
   `klines` costs 1-10). This is a real venue-rate-budget: matches the blueprint's
   `venue-rate-budgeter` part almost exactly, but ccxt's version is *per-endpoint weighted*, not just
   a request counter. Matters because Binance and most venues ban/throttle by weighted cost, not
   request count.

2. **Retry only on `OperationFailed`, never on `ExchangeError`.**
   `exchange.py:Exchange.fetch2` (line 5682) retries up to `maxRetriesOnFailure` times with
   `maxRetriesOnFailureDelay` sleep between attempts, but only catches `isinstance(e, OperationFailed)`
   — i.e. network errors, timeouts, `RateLimitExceeded`, `ExchangeNotAvailable`. A semantic rejection
   (`ExchangeError` and its subtree — `InvalidOrder`, `InsufficientFunds`, `BadRequest`) is re-raised
   immediately, never retried. This is the right split for `order-resubmitter`: retrying an
   insufficient-funds or invalid-order rejection blind is a bug, retrying a timeout is correct.

3. **Two-tier exception hierarchy separates "the request failed" from "the venue said no".**
   `python/ccxt/base/errors.py` (all 273 lines read) defines `BaseError` → `{ExchangeError,
   OperationFailed, UnsubscribeError}`. `ExchangeError` covers deterministic venue rejections
   (`InsufficientFunds`, `InvalidOrder`, `AuthenticationError`, `MarketClosed`,
   `RestrictedLocation`). `OperationFailed` covers transient/infra failures
   (`NetworkError → {DDoSProtection, RateLimitExceeded, ExchangeNotAvailable, InvalidNonce,
   RequestTimeout}`, `BadResponse`, `CancelPending`). This maps almost 1:1 onto the blueprint's
   `order-reject-reason` data type (`balance, rate limit, price band, size step, outage`) — ccxt's
   tree is the reference taxonomy to steal wholesale for `order-reject-classifier`.

4. **Exchange-specific error-message-to-exception mapping table, both exact and substring match.**
   `python/ccxt/binance.py:binance.handle_errors` (line 12383) first checks HTTP status
   (418/429 → `DDoSProtection`), then does substring match on the raw body for known phrases
   (`'LOT_SIZE'` → `InvalidOrder`, `'PRICE_FILTER'` → `InvalidOrder`), then looks up Binance's own
   numeric `code` (e.g. `-1021` → `InvalidNonce`/timestamp skew, `-2010` → `InvalidOrder` /
   `NEW_ORDER_REJECTED`, `-1015` → `RateLimitExceeded` / "too many new orders") against a per-exchange
   `self.exceptions['exact']` / `['broad']` dict, with a special case: `-2015` re-raised as
   `DDoSProtection` when already authenticated once (Binance's documented symptom of a temporary
   IP/key ban, not an actual auth failure) at line 12427. This exact-code table is exactly the shape
   `order-reject-classifier`'s per-venue implementation needs — a static dict, not a heuristic.

5. **Arbitrary-precision decimal arithmetic on strings, no floats.**
   `python/ccxt/base/precise.py:Precise` (all 293 lines read) represents a number as
   `(integer, decimals)` and does add/sub/mul/div/mod purely on integers, converting to/from string.
   `div(other, precision=18)` fixes output to 18 decimal places by default. Every price/amount
   comparison in ccxt goes through `Precise.string_*` static methods rather than `float()`. Directly
   relevant to `position-sizer` and `usdt-pnl-accountant`: float arithmetic on crypto amounts
   (8+ decimal places, sub-cent price ticks) silently accumulates rounding error; this is the
   standard fix.

6. **Price rounds, amount truncates — asymmetric on purpose.**
   `exchange.py:Exchange.price_to_precision` (line 6389) rounds (`ROUND`) to the market's price tick;
   `amount_to_precision` (line 6398) truncates (`TRUNCATE`). Both raise `InvalidOrder` if the result
   rounds/truncates to `'0'` (dust guard) — "amount of {symbol} must be greater than minimum amount
   precision of {precision}". Truncating amount down (never up) means a sized order never asks the
   venue for more size than was actually computed; rounding price to the nearest tick is correct
   because the tick is a discrete grid, not a floor. `position-sizer` and `stop-target-placer` should
   copy this asymmetry rather than rounding both the same way.

7. **Client order ID used for idempotent retries, with a broker/referral tag baked in.**
   `binance.py:binance.sign` (line 12278) auto-generates `newClientOrderId` when the caller doesn't
   supply one, prefixed with a broker id (`x-TKT5PX2F` for spot, `x-xcKtGhcu` for futures) plus a
   UUID, so retried or resubmitted orders are still deduplicatable by the venue and attributable to
   the integration. `order-request` in the blueprint has no explicit idempotency-key field; this is a
   concrete argument for adding one before `order-resubmitter` gets built, otherwise a resubmit after
   a timeout can double-fill.

8. **Nonce = timestamp, with an explicit `recvWindow` anti-replay guard.**
   `binance.py` line 12310: every private request gets `'timestamp': self.nonce()` (line 2946,
   effectively `self.milliseconds()`), plus an optional `recvWindow` (defaults read from
   `self.options['recvWindow']`) that Binance uses server-side to reject a signed request whose
   timestamp has drifted too far from server time. `-1021` (`InvalidNonce`, "your time is ahead of
   server") is a distinct classified error rather than a generic auth failure — clock skew is a real,
   recurring failure mode any live venue adapter needs to detect and alert on separately from
   credentials being wrong.

9. **Endpoint cost varies by parameter, not just by endpoint.**
   `binance.py` line 791: `'depth': {'cost': 2, 'byLimit': [[50, 2], [100, 5], [500, 10], [1000, 20]]}`
   — an order-book fetch costs 2 to 20 rate-limit units depending on the requested depth (`limit`).
   `Exchange.calculate_rate_limiter_cost` in `binance.py` (line 12453) picks the tier by scanning
   `byLimit` for the first bracket the requested `limit` fits under. `order-book-reader` and
   `venue-rate-budgeter` should budget by requested depth, not treat every order-book call as equal
   cost — pulling full depth repeatedly can burn the weekly weight budget fast.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that the part description doesn't yet say |
|---|---|---|
| `ccxt-venue-reader` | `python/ccxt/binance.py` (describe() `api` block, e.g. lines 791-802) | Per-endpoint rate-limit cost, not just "read candles" — the reader needs to declare a cost per call so the shared rate budget can be charged correctly. |
| `ccxt-order-router` | `python/ccxt/base/exchange.py:Exchange.fetch2` (5682) | The retry-only-on-`OperationFailed` split; the part description says nothing about retry policy at all. |
| `order-reject-classifier` | `python/ccxt/binance.py:binance.handle_errors` (12383) | A concrete per-venue exact/broad-match table from raw error code/message to a typed reject reason — the part currently just says "classify", with no worked taxonomy. |
| `order-resubmitter` | `python/ccxt/binance.py:binance.sign` (12278, `newClientOrderId` injection) | Idempotent resubmission needs a client-order-id carried on the order, generated once and reused across retries — not present in `order-request`'s described fields. |
| `venue-rate-budgeter` | `python/ccxt/base/exchange.py:Exchange.init_rest_rate_limiter` (3756) + `binance.py` `calculate_rate_limiter_cost` (12453) | A weighted leaky-bucket, refilled continuously (`refillRate = 1/rateLimit`), not a fixed per-window counter — and cost varies by endpoint and by requested parameters (e.g. `limit`). |
| `position-sizer` | `python/ccxt/base/precise.py:Precise` (whole file) + `exchange.py:amount_to_precision` (6398) | String/integer-based arithmetic and truncate-never-round-up on amount, to avoid float drift and never oversize an order past its computed sizing. |

## New part proposals

- **`order-idempotency-key`** (block: `execution-venue-adapter`) — consumes: `sized-order` (existing);
  produces: `order-request` extended with an idempotency key field, or NEW data type
  `order-idempotency-key: a client-generated id attached to a sized order before it is first sent, so
  a resubmit reuses it and the venue can deduplicate.` Responsibility: stamp every order with a stable
  id once, before it can be retried. Evidence: `python/ccxt/binance.py:12278-12287`
  (`newClientOrderId` generation in `sign`). Obeys T-4: names the data type it produces
  (`order-idempotency-key`), not `order-resubmitter` or any other part.

- **`clock-skew-monitor`** (block: `observability`) — consumes: `fill`, `market-data` (existing, or a
  NEW `venue-time-offset` type carrying measured local-vs-server clock delta); produces: `alert`
  (existing). Responsibility: watch for repeated `InvalidNonce`/timestamp-drift rejections and raise
  before every private order starts failing. Evidence: `python/ccxt/binance.py:2482,2667`
  (`-1021` classified distinctly as `InvalidNonce`, "your time is ahead of server").

## Anti-patterns seen

- `Exchange` (`exchange.py`) is an 8,625-line god object: rate limiting, signing, precision, order
  lifecycle, WS methods, file I/O, and hundreds of `fetch_*`/`create_*` stubs all live on one class.
  Every concrete exchange (`binance.py`, 14,679 lines) subclasses it and adds another few thousand
  lines of endpoint tables and overrides. This is precisely the T-6 violation the transistor rule
  warns about — one part that grew every capability rather than composing many. Steal the mechanisms,
  not the shape: each mechanism above belongs on its own part (rate budgeter, reject classifier,
  precision rounder), never welded onto one adapter class.
- `binance.py:handle_errors` mixes brittle substring matching on the raw response body
  (`body.find('LOT_SIZE') >= 0`) with the structured numeric-code table. The substring checks are a
  workaround for Binance's own inconsistent error responses, not a pattern to copy — `order-reject-
  classifier` should be built on structured codes only, and treat message-string matching as a smell
  that means the venue needs a real code.

## Not useful here

Per the harvest scope, everything outside the four assigned files was skipped: the ~100 other
exchange implementations (`python/ccxt/*.py` besides `binance.py`), the parallel JS/PHP/C#/Go
sources the Python is generated alongside, `python/ccxt/pro/` (websocket streaming variants),
`python/ccxt/async_support/`, all of `examples/`, `doc/`, and every test file. None of that is needed
to extract the rate-limiting, error-classification, precision, and signing mechanisms, which are
representative of the whole codebase (every exchange file follows the same `describe()` + `sign()` +
`handle_errors()` shape as binance.py). ccxt is a REST/WS client library with no backtest, no
position-sizing, no strategy layer, no regime detection — none of that exists here to harvest.
