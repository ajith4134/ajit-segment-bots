# carry replaces funding

**Proposed 2026-09-05.** `leverage-selector` stops reading a perpetual's funding
rate and computes the broker's own cost of borrowing instead. Three orphaned
crypto parts and two data types are retired.

## The gap, and why it was worse than dead code

The user's standing instruction is that a crypto-only part is **replaced with
its Indian analogue**, not left in place and not merely deleted. This one was
left in place, and it was not inert:

    venue-premium-stream-reader -> venue-premium -> funding-rate-forecaster
                                -> funding-forecast -> leverage-selector

Not one link in that chain has an Indian producer, and none ever will — funding
is a perpetual's mechanism. But `_funding_penalty` returned **0.5** for an
absent forecast, and deliberately so: on a perpetual, funding is always charged,
so not knowing the rate is a risk and halving the leverage is the safe reading.

On an Indian segment that same line halves **every** leverage this project
chooses, for a cost nobody levies. Bot 3 would have sized at half the
volatility-implied figure before the broker's cap was even reached, and nothing
would have reported it — the counter would have read `funding_reduced`,
correctly, about a rate that does not exist.

## The analogue, and why it needed no new part

Borrowing costs money for as long as it is borrowed. What is borrowed is the
part of the position the broker's own margin does not cover, and
`broker-margin-requirement` — which the selector already consumes since
`438635b` — states exactly that:

    borrowed fraction = 1 - (margin required / notional) = 1 - 1/leverage
    carry per day     = borrowed fraction x the broker's daily rate

So the carry is **computed, not forecast**. No part had to be built to predict
it, no new data type was needed, and the thing that used to be a forecast with
an unknown value is now arithmetic over a fact.

## Zero is the finding, not a placeholder

`intraday_borrowing_daily_interest_rate` is **0.0**. Upstox's intraday (MIS)
leverage carries no financing charge for a position opened and squared off in
the same session. Interest is what the **margin trading facility** charges to
carry a position overnight — a different product this segment deliberately does
not use, and `intraday-square-off-placer` exists to guarantee it never does.

That is precisely why the crypto penalty could not simply be pointed at an
Indian input. A perpetual always pays funding; an intraday equity position
squared off the same day genuinely pays none. Keeping a penalty shaped for the
first would have been the same defect wearing an Indian name.

The mechanism is wired for the day that changes: set the real MTF rate and
leverage reduces exactly the way funding used to. Not verified against Upstox's
brokerage schedule — same standing as the 15:15 square-off time — so confirm it
there before live.

**Removing the 0.5 does not remove the protection it stood in for.** A missing
broker quote on a borrowing segment is still `UNLEVERAGED_NO_BROKER_QUOTE`, and
`broker_margin_requirement_maximum_age_seconds` (7,200) makes a stale grant
expire into that same refusal rather than stand forever.

## What is retired

`funding-rate-forecaster` and `venue-premium-stream-reader`, with the types
`funding-forecast` and `venue-premium`. The blueprint edit **refuses to retire a
type that anything still produces or consumes** rather than trusting the author
— a type is retired when nothing needs it, never to make a diff smaller.

Their modules and tests are deleted, matching the convention
`whale-transfer-reader` and `social-sentiment-reader` set when their last
consumers stopped reading them: gone from the tree, kept in git history.

`github-strategy-miner` advertised `funding-forecast` in
`available_data_kinds` — what this project can feed a mined strategy — and would
have gone on judging strategies testable on data nothing carries. It now
advertises `broker-margin-requirement`.

## Still crypto-only, named rather than quietly left

`funding-settlement-recorder` produces `funding-settlement`, read by
`usdt-pnl-accountant` and `pnl-attributor`. It is the same perpetual mechanism
and it is **not** retired here, because two live parts read it and cutting it
without replacing their input is the "just delete" this instruction forbids.

## Verification

    python3 dashboard/check_contracts.py
    .venv/bin/python -m pytest tests/parts tests/runtime tests/operate
