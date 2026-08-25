# A conclusion nobody records is a conclusion nobody has

Proposed by Claude 2026-08-25, from phase 5. The operator asked for the trade
attribution on the board; it cannot be put there, and the reason is a gap in the
blueprint rather than in the board.

## The finding

`pnl-attributor` splits a closed trade into direction, timing, size, fees, funding,
slippage and a residual that reconciles by construction. It ran on a real closed
trade on 2026-08-25 and produced exactly that, with a largest residual share of
0.32 — which by its own docstring is the most useful thing it emits, because it
says the model of where PnL comes from is missing something.

That attribution then vanished.

`pnl-attribution` is produced by one part and consumed by five —
`trade-episode-encoder`, `lesson-extractor`, `loss-cause-classifier`,
`expectancy-decomposer`, `reward-shaper`. **No recorder consumes it**, so it exists
only on the bus, only while the producing process lives, and only for parts running
at that moment. A board cannot show it because there is nothing on disk to show. A
part started tomorrow cannot learn from it because it is gone.

The same is true of every conclusion this block draws: `trade-episode`,
`loss-cause`, `stop-audit`, `exit-quality`, `entry-quality`, `excursion-profile`.
Twenty parts were switched on to score trades and not one of their scores survives
the process that computed it.

`pnl-attributor`'s own docstring says the conversion rate is "journalled (RL-029)".
Journalling was intended. The contract never wired it.

## Why this is the blueprint's problem and not the board's

Under R-01, edges are computed from consumes and produces. A board reading
`pnl-attribution` off the bus would be a reader the blueprint does not declare, and
a board reading it out of a part's private memory would be worse. The honest fix is
that something *declares* it consumes the attribution and writes it down.

That something already exists and already has the role. `learning-recorder`'s stated
job is to **journal what was learned, researched, forecast, scored**. An attribution
is what was scored. It consumes `research-finding`, `forecast-accuracy`,
`ablation-scorecard`, `skill-usefulness`, `opportunity-instruction` and
`decision-rationale` — six kinds of conclusion — and not the one this block's whole
purpose is to produce.

## The change

`learning-recorder` gains `pnl-attribution`.

One data type, one part, no new part and no new type. The recorder already produces
`journal-entry` and already writes a digest-chained journal, so nothing about how a
conclusion is recorded changes — only whether this one is.

## What is deliberately not in this change

**Not `trade-episode`.** An episode is an assembly of six other conclusions and is
superseded whenever one of them lands; journalling every version would write the
same trade many times over, and the encoder was re-encoding one trade 3,048 times
until 2026-08-25 for exactly that reason. What an episode should record, and whether
it is the final one or each supersession, is a separate question that deserves its
own answer rather than being decided as a side effect of this.

**Not the other five conclusions.** `loss-cause`, `stop-audit`, `exit-quality`,
`entry-quality` and `excursion-profile` have the same gap and the same likely
answer. They are left out because each one is a claim about what deserves to be
permanent, and making five such claims in one edit to satisfy one board request is
how a blueprint drifts. The attribution is the one the operator asked for and the
one already named as journalled in its own part's docstring.

They are named here so the gap is recorded rather than rediscovered.

## What this makes possible

The trade board can show, per closed trade, where the money actually came from —
and the residual beside it, which is the number that says how much of the answer is
missing. A board that showed only the components would present a reconciliation as
complete when its own author says it may not be.

## How it is checked

`python3 dashboard/check_contracts.py` after the edit. `learning-recorder` must
appear as a consumer of `pnl-attribution` in the wiring explorer's data-type view,
and `pnl-attributor` must gain a second outgoing edge there.

The part must then be on the spine, which it is not today — a recorder that
declares the consume and never runs records nothing, and would be the same gap
wearing a contract.
