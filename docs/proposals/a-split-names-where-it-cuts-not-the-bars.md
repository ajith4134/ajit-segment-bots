# instruction-replayer reads the bars it replays over

**Proposed by Claude, 2026-08-25, during the payload-shape sweep.**

## What was found

`instruction-replayer` replays an instruction over a walk-forward split's
out-of-sample period. It needs two things: where the split cuts, and the bars in
that range. It consumes `walk-forward-split`, which carries the first and not the
second -- `WalkForwardSplit` is `split_id`, `fold`, four timestamps and an
embargo. The part read:

    window = getattr(split, "window", None)

which no producer has ever supplied, so every split was counted as arriving
without bars and **nothing has ever been replayed**. The part's own docstring
says so, and names this edit as the fix:

> *A split names where it cuts and not the bars it cuts; the bars are on
> historical-window, which this part is not given. Until a blueprint edit gives it
> the window -- or the split carries one -- every split arrives without bars.*

A second defect sits behind it: `walk-forward-splitter` publishes a `SplitOutcome`
(a window id, a state, and a **tuple of splits**), and the reader treated each
payload as a single split -- reading `split.split_id` and `split.test_from_ns` off
the wrapper. Even given bars, nothing would have replayed.

## The change

    instruction-replayer  consumes += historical-window

`historical-window` is produced by `historical-bar-store` and already consumed by
`walk-forward-splitter`, which is what makes the two halves joinable: an outcome
names the `window_id` it split, and that is the window's own id.

## What the code then does

For each `SplitOutcome`, look up the window it names; for each `WalkForwardSplit`
inside it, replay every instruction over the test period. A split whose window has
not arrived is still counted rather than dropped -- the bars may simply not have
been built yet, and a replay against bars that are missing is exactly what RL-071
and this block exist to prevent.
