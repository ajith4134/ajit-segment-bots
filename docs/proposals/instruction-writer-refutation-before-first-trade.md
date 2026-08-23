# The writer demands a verdict nothing upstream of it can give

**Proposed 2026-08-23, while making the hypothesis block launchable.**

## What was found

`instruction-writer` writes an opportunity instruction only when every one of
its conditions agrees, and one of them is *a refutation battery did not break
it*. In the blueprint the only producer of `refutation-verdict` is
`causal-refutation-battery`, and it consumes `instruction-scorecard` -- the
record of an instruction that has already traded. The writer does not consume
`refutation-verdict` at all; its twenty inputs do not include it, and by R-01
its `start_part` may not bind it.

So a hypothesis that has never traded cannot carry a verdict, and the writer
refuses every first-time hypothesis with `nothing-tried-to-break-it`. The part
is launchable, its refusal is published by name, and by construction it writes
nothing. The same holds, less severely, for the regime tag: the writer does not
consume `hypothesis-regime-tag`, so only a hypothesis that names its regime
itself (a mutation or an inversion) can pass `nothing-says-which-market-this-
is-a-claim-about`; a mined formula never can.

The `start_part` does not invent a verdict to get past this. A verdict typed
in at the point of use would be indistinguishable, in the record, from one the
battery produced.

## What has to change, and it is a blueprint edit first

One of two, decided by the user (RL-016), neither taken here:

1. **The writer's refutation condition is a promotion condition, not a
   writing one.** `instruction-promotion-gate` already consumes
   `refutation-verdict` and `required-sample-size` and `falsification-
   criterion`; the battery runs on the paper record the instruction produces.
   Drop `WAS_REFUTED` / `NOT_REFUTATION_TESTED` from the writer's conditions,
   and the writer becomes the gate into paper testing, the promotion gate the
   gate out of it. No wiring change.

2. **A pre-trade refutation exists and the writer consumes it.** Add
   `refutation-verdict` and `hypothesis-regime-tag` to the writer's `consumes`
   in `docs/features.json` through `dashboard/blueprint_edits/`, and give the
   battery an input that is not a scorecard. A larger change, and it makes the
   battery judge a claim before any trade exists, which is what the falsifier's
   criterion already does.

Until one is taken, `instruction-writer` runs and refuses, and the
hypothesis block produces evidence that stops at its last gate.
