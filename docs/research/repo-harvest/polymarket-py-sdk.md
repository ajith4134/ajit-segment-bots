# Polymarket/py-sdk — read 2026-08-20

MIT licence (Copyright 2026 Polymarket). Python (3.11+, uses `httpx`, `eth_account`, `eth_abi`,
`pydantic`). Official SDK for Polymarket's CLOB (central limit order book, prediction markets on
Polygon), covering REST, websockets, gasless relayer transactions, and RFQ. Commit
`c8fb84bb51e60f790239056be7be0f5cc337d2e0`.

Note: `python/ccxt`-style top-level files (`auth.py`, `calls.py`, `rate_limit.py`, `rfq.py`,
`transactions.py`) are thin public re-export shims; the real implementation lives under
`src/polymarket/_internal/`, which is what this note cites.

## Mechanisms worth stealing (ranked)

1. **Two-tier signing: L1 wallet EIP-712 to mint an API key, L2 HMAC to authenticate every request.**
   `src/polymarket/_internal/l1_auth.py:build_api_key_auth_typed_data`/`sign_api_key_auth` sign a
   fixed message ("This message attests that I control the given wallet") via EIP-712
   (`domain: ClobAuthDomain`) once, to derive an `ApiKeyAuthSignature`. After that, every ordinary
   request is authenticated with `src/polymarket/_internal/hmac.py:build_hmac_signature`:
   `HMAC-SHA256(secret, f"{timestamp}{method}{path}{body}")`, base64url-encoded. The expensive wallet
   signature happens once; day-to-day auth is cheap HMAC. Directly reusable pattern for any venue
   adapter that needs a wallet-backed but low-latency signing path.

2. **Order EIP-712 typed data with builder attribution and EIP-1271 (smart-wallet) support baked
   into the struct.** `src/polymarket/_internal/actions/orders/typed_data.py:build_order_typed_data`
   signs an `Order(uint256 salt, address maker, address signer, uint256 tokenId, uint256 makerAmount,
   uint256 takerAmount, uint8 side, uint8 signatureType, uint256 timestamp, bytes32 metadata,
   bytes32 builder)` struct. `signature_type == 3` (POLY_1271) triggers a different `TypedDataSign`
   wrapper (lines 74-95) for smart-contract wallets, and `build_order_signature` (line 98) appends a
   domain-separator + contents-hash + type-string trailer per EIP-1271. The `builder` field is a
   first-class part of the signed struct, not an out-of-band tag — attribution survives on-chain.
   Relevant to `order-request`: carrying an attribution/idempotency field inside the signed payload
   itself (not just as a side-channel header) is stronger than ccxt's client-order-id-in-params
   approach (see `ccxt.md`).

3. **Order book walked to resolve a slippage-bounded market price before sizing, with FOK vs FAK
   semantics.** `src/polymarket/_internal/actions/orders/estimate.py:_calculate_buy_market_price` /
   `_calculate_sell_market_price` walk the book from the *best* price outward (`reversed(asks)`),
   accumulating notional/shares until the requested amount is covered, and return the price level at
   which it clears. If liquidity runs out: `order_type == "FOK"` raises `InsufficientLiquidityError`
   immediately; `"FAK"` instead returns the worst level reached (partial fill accepted). The
   resolved price is also validated against `book.tick_size` bounds (line 174). This is close to a
   textbook `execution-cost-model`: walking the real order book to price a market order rather than
   assuming top-of-book fill is exactly the missing mechanism the blueprint's `cost-estimate` needs
   for anything beyond a flat slippage constant.

4. **Full jittered exponential backoff, shared by REST retry-callers and websocket reconnect.**
   `src/polymarket/_internal/ws/backoff.py:jittered_backoff(attempt, base_s=0.25, max_s=30.0)`
   computes `cap = min(base_s * 2**min(attempt,64), max_s)` then returns `random() * cap` (full
   jitter, not decorrelated jitter). `src/polymarket/_internal/streams/reconnect.py:ReconnectScheduler`
   wraps it into a stateful scheduler: `schedule()` no-ops if already pending or `should_reconnect()`
   is false, `reset()` on a successful reconnect zeros the attempt counter, `stop()`/`aclose()` cancel
   any pending timer. One reusable backoff primitive used for both HTTP retries and WS reconnects —
   the blueprint's `venue-outage-rider` and `order-resubmitter` should share one such primitive rather
   than inventing separate retry math per part.

5. **The SDK explicitly does NOT auto-retry — it surfaces server state and lets the caller decide.**
   `src/polymarket/errors.py:RequestRejectedError` docstring (line 59): "The SDK does not retry
   automatically; callers decide how to react." `retry_after` is parsed from either the `Retry-After`
   header or a `retry_after_seconds` JSON field (`clients/_transport.py:_extract_retry_after`, line
   415) and attached to the exception, but nothing loops. Rate-limit state (`Poly-RateLimit-Remaining`,
   `-Reset`, `-Tier`, `-Warning` headers) is parsed every response and pushed to an optional listener
   callback (`_notify_rate_limit_update`, line 376) rather than acted on internally. This is the
   opposite design choice from ccxt's automatic `fetch2` retry loop (see `ccxt.md` item 2) — worth
   flagging explicitly for `order-resubmitter`: deciding *whether* to resubmit is risk/control-plane
   business, not transport plumbing, and Polymarket's SDK keeps that decision one layer up.

6. **Trading-restriction states surfaced as a typed enum, distinct from a generic error.**
   `src/polymarket/errors.py:TradingRestriction = Literal["restarting", "cancel_only", "post_only"]`
   (lines 38-45) and `clients/_transport.py:_detect_trading_restriction` (line 441) read HTTP 425
   ("restarting" — matching engine down, no orders accepted) vs 503 with a JSON body naming the
   restriction. This is a venue-level halt state distinct from a per-order rejection — maps onto
   `trading-halt` / `market-anomaly` more than `order-reject-reason`, and the three-state vocabulary
   (`restarting` / `cancel_only` / `post_only`) is a concrete example of T-5-style explicit, closed
   states for something the blueprint currently only has as a boolean `trading-halt`.

7. **On-chain allowance checked as a distinct precondition before sizing/signing an order.**
   `src/polymarket/_internal/actions/orders/allowance.py:fetch_current_allowance` calls the CLOB's
   balance/allowance endpoint (`_account_actions.build_balance_allowance_request`) keyed by
   `COLLATERAL` (for a BUY) or `CONDITIONAL` + `token_id` (for a SELL), and returns the raw allowance
   int for a given spender contract. `errors.py:InsufficientAllowanceError` is a distinct exception
   from `InsufficientLiquidityError` (no resting book depth) and `InsufficientFunds`-equivalent
   (no balance) — three separate failure causes that a naive integration would conflate into one
   "order rejected" bucket.

8. **Gasless relayer retry distinguishes genuinely-retryable submit failures from nonce conflicts,
   and recovers the correct on-chain nonce from a failed submit rather than guessing.**
   `src/polymarket/_internal/actions/relayer/submit.py` (imported into `gasless.py`) exposes
   `is_retryable_submit_error` and `onchain_nonce_from_submit_error` — a failed meta-transaction
   submit is inspected to decide (a) whether it's safe to retry at all, and (b) if it is, what nonce
   to retry with, read back from the error rather than blindly incrementing a locally-tracked counter
   that may have drifted. `GASLESS_SUBMIT_RETRY_ATTEMPTS` bounds the loop.
   `gasless.py` orchestrates three distinct signing shapes for the relayer (deposit-wallet batch,
   proxy transaction, Gnosis Safe multisend) behind one submit path. Relevant to any
   `order-resubmitter`/`order-request` design that has to survive nonce desync after a partial
   failure — read the actual chain/venue state back rather than trusting local bookkeeping.

9. **Asymmetric decimal rounding, explicit per direction, distinct from ccxt's truncate-only amount
   rule.** `src/polymarket/_internal/actions/orders/math.py`: `round_down` (`ROUND_FLOOR`),
   `round_up` (`ROUND_CEILING`), `round_normal` (`ROUND_HALF_EVEN`) are three named functions, not one
   generic rounder with a flag — callers pick the direction that matches which side of the trade
   they're on (e.g. round a BUY notional up, a SELL proceeds down, to never let the SDK compute in
   the trader's favor by accident). `parse_amount` scales to `_COLLATERAL_DECIMALS = 6` (USDC has 6
   decimals) using `ROUND_HALF_EVEN`, matching how the underlying collateral token actually quantizes
   on-chain.

## Map onto existing parts

| existing part id | repo file (reference implementation) | what the repo does that the part description doesn't yet say |
|---|---|---|
| `execution-cost-model` | `src/polymarket/_internal/actions/orders/estimate.py:_calculate_buy_market_price` / `_calculate_sell_market_price` (186-213) | Concrete algorithm for walking a real order book to a slippage-bounded fill price, with FOK-refuse vs FAK-accept-partial as two distinct outcomes — the part currently only says "charge each simulated fill what it would really have cost," with no worked method. |
| `order-reject-classifier` | `src/polymarket/errors.py` (`RequestRejectedError`, `RateLimitError`, `InsufficientLiquidityError`, `InsufficientAllowanceError`, all distinct exception types) | A finer-grained taxonomy than a generic "reject reason" — separates allowance, liquidity, and rate-limit as different causes needing different remediation (approve more allowance vs. wait vs. reduce size). |
| `venue-rate-budgeter` | `src/polymarket/clients/_transport.py:_parse_rate_limit_headers` (342) | Server-reported remaining/reset/tier/warning state read from response headers every call, rather than a client-side estimate — the part as described computes the budget locally; this repo shows reading it authoritatively from the venue instead. |
| `order-resubmitter` | `src/polymarket/_internal/actions/relayer/submit.py` (`is_retryable_submit_error`, `onchain_nonce_from_submit_error`) | Recovering the correct nonce from a failed submit's error response, rather than tracking it locally — relevant wherever a resubmit could otherwise double-submit or permanently desync. |
| `trading-halt-decider` | `src/polymarket/errors.py:TradingRestriction` (38) + `clients/_transport.py:_detect_trading_restriction` (441) | A closed three-state vocabulary for a partial-halt (`cancel_only`, `post_only`, `restarting`) rather than a binary halted/not-halted — worth folding into the halt states this part reasons over. |

## New part proposals

- **`order-book-walk-pricer`** (block: `backtesting` or `execution-venue-adapter`) — consumes:
  `order-book-snapshot` (existing); produces: NEW `walked-fill-price: the price level reached by
  accumulating book depth from best price outward until a requested notional or share count is
  covered, with a flag for whether it cleared fully.` Responsibility: turn a raw book snapshot into
  a realistic fill price for a given order size, distinct from `execution-cost-model`'s job of then
  charging fees/spread on top. Evidence:
  `src/polymarket/_internal/actions/orders/estimate.py:_calculate_buy_market_price` (186).
  Obeys T-4: names the data type it produces, not any consuming part.

- **`onchain-allowance-checker`** (block: `risk-capital-allocation`) — consumes: NEW
  `venue-allowance: the amount a spender contract is currently approved to move on a trader's behalf,
  read from the venue rather than assumed.`; produces: `risk-limit` (existing, as a hard zero when
  allowance is insufficient). Responsibility: refuse sizing an order past what is actually approved
  on-chain, distinct from balance or liquidity checks. Evidence:
  `src/polymarket/_internal/actions/orders/allowance.py:fetch_current_allowance` (10);
  `src/polymarket/errors.py:InsufficientAllowanceError` (129). Only relevant if the segment ever
  trades against an on-chain venue (e.g. an options/perp DEX) rather than a centralized exchange —
  ccxt-style CEX venues don't have this failure mode.

## Anti-patterns seen

- None rising to a T-1..T-6 violation. The codebase is unusually well-factored for a client SDK:
  narrow files under `_internal/actions/orders/` (each under 100-200 lines, one responsibility —
  `math.py` only rounds, `estimate.py` only prices, `allowance.py` only reads allowance), sync/async
  variants kept as thin parallel functions rather than one function branching on a flag. Nothing here
  resembles a god object; it reads like a system already built close to the transistor shape.

## Not useful here

Most of the RFQ/combo-order machinery (`src/polymarket/rfq.py`, `_internal/actions/combo_rfq.py`,
`_internal/actions/combos.py`) and the perps module (`_internal/actions/perps/`) are Polymarket-
specific to its own product surface (prediction-market shares, not spot/futures/options as this
project defines them) and were only skimmed for state-machine shape, not harvested as mechanisms —
the underlying data types (`RfqStatus`, position ids, combo condition ids) don't map onto anything in
the blueprint. The websocket stream protocol modules (`_internal/streams/clob/`, `rtds/`, `sports/`)
were not read beyond `reconnect.py`/`backoff.py` — they're Polymarket wire-protocol parsers with no
generalizable mechanism beyond the backoff/reconnect scheduler already covered above. Frames/pandas/
polars integration (`frames/`), Jupyter repr helpers (`_jupyter.py`), and all of `docs/`, `examples/`,
and the test suite were not read — client convenience layers, not trading mechanisms.
