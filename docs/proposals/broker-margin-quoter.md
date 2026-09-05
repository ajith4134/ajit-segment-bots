# broker-margin-quoter

**Proposed 2026-09-05.** Asks the broker what margin an intraday order on each
universe instrument requires, and publishes the leverage that implies as
`broker-margin-requirement`.

## The gap

Bot 3 trades cash equity intraday on leverage. `segments/cash-equity-intraday.toml`
states `leverage_ceiling = 5.0` because the operator asked for it — but that is a
ceiling this project imposes, not a limit it has verified. The real limit is the
broker's, set per stock from SEBI VAR+ELM margins that change daily, and it is
always the smaller of the two that binds. Nothing in the system could find it out.

**It cannot be read from the instrument master.** Measured 2026-09-05 against
Upstox's real file:

    rows in master                     102,789
    rows carrying `intraday_margin`          0

`InstrumentListing.intraday_margin_percent` is therefore always `None` — a field
the adapter parses that the source never carries. Anything built on it would
have read `None` forever and defaulted to an invented number.

The one place that knows is `https://api.upstox.com/v2/charges/margin`, and the
adapter already speaks it (`margin_endpoint_url`,
`build_margin_quote_request_payload`, `read_margin_quotes`, all written
2026-09-01 and never called).

## Per instrument, not per order — which removes the circularity

The obvious design quotes each candidate order, and it does not work:
`position-sizer` needs the leverage in order to choose a size, so a quote that
needs a size first is a loop.

It is also unnecessary. Upstox's requirement scales with quantity, so the
**ratio** does not:

    leverage available = notional quoted / margin the broker requires

So the quote asks for the lot the instrument actually trades in, takes the
ratio, and publishes that. Quoting the universe on an interval answers the same
question as quoting every order, without a network call inside the trade path
and without spending the rate limit on every intent.

## A silent broker is unlevered, never the ceiling

`leverage-selector` gains `broker-margin-requirement` as a **hard cap**, applied
after the operator's ceiling so that whichever is smaller shows in the outcome.
Two new outcomes, deliberately distinct:

| | |
|---|---|
| `AT_CEILING` | the operator's own limit — a policy this project chose |
| `AT_BROKER_LIMIT` | what the broker will lend — a fact about the account |

A board that showed them as one number could not tell you which one to change.

`UNLEVERAGED_NO_BROKER_QUOTE` is the third: on a segment that borrows, a missing
quote means **unlevered**, because the alternative is sizing against a
permission nobody gave. A margin endpoint that is down must not silently become
5x. On a segment that does not borrow — a bought option has no margin to quote —
its absence is correct rather than missing, which is the same not-a-gap reading
three earlier audits reached about this part, and `a_broker_quote_is_required`
is what separates the two.

`net_buy_premium` is excluded from the margin total: it is the cash cost of
buying an option, not margin lent against, and counting it would make a bought
option look like it needed margin it does not.

## What this does not do

It does not replace `funding-forecast`. `leverage-selector` still consumes it,
still halves the leverage when it is unknown, and there is still no Indian
producer of it — the analogue is the broker's intraday interest on the borrowed
portion, which is a different input and a separate piece of work. For the
cash-equity segment that penalty currently applies with no funding producer
running, so the chosen leverage will sit at half the volatility-implied figure
before the broker's cap is even reached. That is conservative rather than
dangerous, and it is named here so it is not mistaken for finished.

It has also never been called against the real endpoint. Every test uses the
documented response shape and a real captured request payload; the live call
needs credentials and an open market. Verify at the first session bot 3 runs.

## Verification

    python3 dashboard/check_contracts.py
    .venv/bin/python -m pytest tests/parts/broker_adapter tests/parts/risk_capital_allocation

and live: `calls_made` climbing with `calls_failed` at zero,
`quotes_read` matching, and `leverage_available` landing near 5 for a liquid
large cap. `calls_made` at zero while `universe_entries_seen` climbs is its own
fault — a universe nobody can be quoted for — and must not be read as a broker
that answered badly.
