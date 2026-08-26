# The instruction chain had a zero-instruction fixed point, and it held

**Written 2026-08-26, after measuring the whole chain on the live spine.**

The universal opportunity scanner held **zero watch conditions**. It had swept 455
times, skipped 42 symbols it already held, and produced 0 candidates — because a
sweep against zero conditions is a sweep that cannot produce anything, and its
slowest pass took 0.997 milliseconds, which is what an empty answer costs.

Tracing back from there found not one break but six, each of which alone was
sufficient to keep the count at zero.

## What was measured

    universal-symbol-sweeper   455 sweeps, 0 candidates, 0 conditions received
    watch-condition-compiler   messages_received {} -- nothing, ever
    instruction-writer         requests 479,323   refused 479,323   written 0
    hypothesis-ranker          hypotheses_ranked 0, not_enough_inputs 602,112,
                               and 602,112 priorities published anyway
    hypothesis-deduplicator    candidate-formula 228 received, hypotheses_scored 0
    instruction-promotion-gate decisions 0
    symbolic-hypothesis-miner  226 formulas returned, best held-out excess 0.41

## The six breaks

**1. A field the producer has never carried.** `hypothesis-deduplicator._shape_of`
looked for `measurement` and a `context` mapping. `CandidateFormula` — its only
live producer — carries neither; it carries `terms`. Every one of the 228 formulas
returned None and was skipped, so **not one `novelty-score` was ever published**.
The reads went through `getattr(..., None)` defaults, which is precisely the shape
`check_payload_reads.py` cannot see: a default turns a field nobody carries into a
value everybody accepts. This is the same shape as the live-money guard judging
three of its four tests against getattr defaults.

**2. A ranker that cannot rank publishes anyway.** `information_per_trade` needs an
expected edge, and the only sources of one are `expectancy-breakdown` and
`instruction-scorecard` — both records of something that has **already traded**. A
never-traded hypothesis has no edge, so the ranker returned None for every one of
them, counted `not_enough_inputs`, and **published a priority regardless**. Three of
the writer's conditions read novelty, the trades required and the edge half-life
off those priorities, so three conditions failed on the ranker's inability to rank
rather than on anything about the hypothesis.

**3. Refutation before the first trade — the deadlock proper.** The writer required
a refutation verdict. The only producer of `refutation-verdict` is
`causal-refutation-battery`, which consumes `instruction-scorecard`: the record of
an instruction that has already traded. So a hypothesis that has never traded could
not carry a verdict, no instruction was written, nothing traded, and no verdict
could ever exist. **Zero instructions is a stable fixed point of the pipeline, and
nothing in the pipeline could leave it.** This was found and written up on
2026-08-23 in `instruction-writer-refutation-before-first-trade.md` and left for
the user to decide; option 1 is taken here.

**4. A regime tag nobody delivered.** The writer refused anything without a regime
tag, did not consume `hypothesis-regime-tag`, and mined formulas name no regime of
their own. `hypothesis-regime-tagger` was publishing 228 tags to nobody.

**5. A vocabulary discovery and scanning did not share.** `signal-outcome-labeller`
stamped each detector's own evidence as the training label's features —
`spread_z`, `hedge_ratio`, `reversion_strength` for the pair detector — so
`symbolic-hypothesis-miner` mined formulas over names meaningful only inside the
detector that produced them. A pair's spread z-score is not a property of a symbol.
`watch-condition-compiler` knew four measurement names and the miner named none of
them, so **even a hypothesis that passed every other condition would have been
refused as `NO_MEASUREMENT`, and a condition compiled from one could never have
been evaluated on an arbitrary symbol.** RL-009 asks for whatever the system proves
to be tested everywhere; a claim stated in one detector's private terms cannot be.

**6. A property called as a method.** `writer.live_instructions()` — a `@property`
returning a tuple — would raise `TypeError: 'tuple' object is not callable` the
first time a `regime-break-alert` arrived with `has_broken` set. Latent only
because no instruction existed to retire.

## What changed

- `hypothesis-deduplicator` reads a formula's `terms`. A conjunction is refused
  rather than shaped by its first term, which is the same answer the writer gives
  it and for the same reason: the scanner watches one comparison, not a conjunction.
- `hypothesis-ranker` gains `candidate-formula` and reads `held_out_excess` as the
  pre-trade expected edge — measured on data the formula was not fitted on.
- **`instruction-writer` becomes the gate INTO paper testing.** Refutation moves to
  `instruction-promotion-gate`, which already consumes `refutation-verdict`,
  `required-sample-size` and `falsification-criterion`, and which judges the paper
  record the instruction produces. The writer gains `novelty-score`,
  `required-sample-size` and `hypothesis-regime-tag` so each of its conditions is
  read from the part that measures it rather than relayed.
- **`runtime/sweep_measurements.py` becomes the contract between discovery and
  scanning**, widened from four names to fifteen, and `signal-outcome-labeller`
  stamps that vocabulary onto every label. What the miner may mine over is now, by
  construction, what the scanner can watch.
- The property call is fixed.

## What did not change, deliberately

**The writer still refuses a hypothesis whose regime tag says no regime has enough
measured evidence yet.** The tagger's own words: an untagged claim is a claim about
every market and almost none are. 214 of the 228 mined formulas are untagged today,
and that number falls as held-out trades accumulate. Weakening the bar to admit them
would be inventing the evidence the bar exists to require.

**A tag naming several regimes is refused by name and counted**
(`SEVERAL_REGIMES_NEEDS_AN_INSTRUCTION_EACH`) rather than collapsed to its first
regime, which would silently narrow the claim, or to None, which would widen it.
The right answer is one instruction per regime, and that is a change with its own
`instruction_id` shape. Until it is made this is a number on the board.

## Still open, found while doing this and not fixed

- **`signal-outcome-labeller` does not consume `market-regime`**, so `claim.regime`
  is the default `"any"` on every label it has ever built and `TrainingLabel.regime`
  is a constant. The regime tagger works around it by splitting a formula's held-out
  trades across the regimes current at mining time, which is weaker than reading the
  regime the evidence actually came from. A blueprint edit of its own.
- **`liquidation-cluster-mapper` publishes 544,798 maps with zero clusters**, every
  one `refused-no-margin-schedule`. Its `maps_published` standing reads 0 while the
  bus counted 544,798 — the wire carries, and carries nothing. Research on
  2026-08-26 found the cause is venue-specific: Bybit's
  `GET /v5/market/risk-limit` is public and unauthenticated, Binance's
  `GET /fapi/v1/leverageBracket` is **signed**.
- **`largest_z` and `largest_burst_z` are updated after every rejection**, so they
  read 0.0 across 660,000 observations. Peak-of-what-fired, not peak-observed —
  there is no way to see how close anything came.
