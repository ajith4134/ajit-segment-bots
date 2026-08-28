# Nine detectors that never learn whether they were right

**Written 2026-08-28, after tracing why `bull-feature-builder` had produced zero
complete feature vectors in its entire life.**

Every scanner detector carries a `SignalCalibrator`. Its docstring states what it
is for:

> What actually happened after this detector's past calls (RL-060). Kept per
> detector and per regime, because a detector that works in a trending market and
> fails in a choppy one has two different hit rates, and one number over both
> describes neither.

Nine detectors define the method that feeds it. **Nothing calls any of them.**

    funding-skew-detector          observe_outcome    outcomes_learned  0
    liquidation-cascade-detector   observe_outcome    outcomes_learned  0
    mean-reversion-detector        observe_outcome    outcomes_learned  0
    momentum-burst-detector        observe_outcome    outcomes_learned  0
    sentiment-shift-detector       observe_outcome    outcomes_learned  0
    spread-reversion-detector      observe_outcome    outcomes_learned  0
    universal-symbol-sweeper       observe_outcome    outcomes_learned  0
    volatility-gap-detector        observe_outcome    outcomes_learned  0
    whale-flow-detector            observe_outcome    outcomes_learned  0

The tests for all nine call it, which is why the suite is green. Nothing in the
running system does, and nothing could: none of the nine declares an input that
carries an outcome, so no `start_part` has anything to call it with.

Nothing reports this as a fault, because nothing failed. Every detector reports
its zero honestly and no probe reads a zero there as wrong. It simply never
happened, for as long as the system has existed.

## What it costs

`EntryCandidate` keeps two numbers apart on purpose — `signal_strength` is how
unusual the observation is, `confidence` is how often that has meant anything.
The second comes from the calibrator, so it is the prior on every candidate ever
raised, and `is_calibrated` has always been False.

That absence lands on the conviction model as a missing feature. Measured on the
live spine on 2026-08-28:

    bull-feature-builder   2,828 vectors built   detector_hit_rate missing on 100%
    bear-feature-builder   3,094 vectors built   detector_hit_rate missing on 100%
    complete_vectors                          0  for both, since the parts were written

    bull-conviction-model  convictions_formed                    2,742
                           forecasts_the_gate_could_not_judge    2,612

    bull-opinion-composer  stood_down_by_reason
                             .conviction-below-threshold         1,690
    bear-opinion-composer  same reason                           1,030

So the model that decides whether to trade is asked to weigh a detector's claim
without ever being told how often that detector has been right, and the detector
goes on raising candidates at its prior for ever. Nothing in the chain degrades
gracefully: it produces a number, and the number is a guess that can never
improve.

## The outcome already exists on the bus

This does not need a new measurement. `signal-outcome-labeller` was built to
answer exactly this question and is running now:

> **signal-outcome-labeller: did the move a detector expected actually happen?**
> A claim resolves *right* when the price moves the threshold in the claimed
> direction first, *wrong* when it moves the threshold against first, and
> *unresolved* when the horizon expires with neither reached. An unresolved claim
> is dropped, never labelled false.

    signal-outcome-labeller   claims_opened        34
                              labels_published      7
                              resolved_right        5
                              resolved_wrong        2
                              measured_hit_rate  0.714
                              by_detector.spread-reversion-detector  34

It reads `entry-candidate`, watches the prices that follow, and publishes
`training-label` carrying `detector`, `regime`, `direction` and
`label_for(THE_SETUP_WAS_RIGHT)`. That is the detector's hit rate, already
measured, already on the bus, already keyed by the detector that made the claim.

**It travels to the conviction models and to the two profilers. It has never
travelled back to the nine parts whose claims it is judging.**

That is the whole defect. Not a missing measurement — a missing edge.

## What this changes

**Blueprint.** `training-label` is added to the `consumes` of all nine detectors.
That is the entire registry change; every one of them keeps its existing produces
and its existing states.

    dashboard/blueprint_edits/apply_2026-08-28_the_detectors_read_their_own_record.py

**Payload.** `EntryCandidate` gains one field, `calibration_key`: the key this
detector calibrates on, stated by the detector at the moment it raises the claim.
`signal-outcome-labeller` carries it onto `TrainingLabel`. Nothing else changes
shape.

**Code.** Each detector's `start_part` reads `training-label`, keeps the ones
naming itself, and calls `observe_outcome(label.calibration_key, was_right)`.
Nine identical readers, no per-detector special case.

### Why `calibration_key` rather than reusing `regime`

Because seven detectors calibrate on the regime and two do not, and the
calibrator cannot tell:

    funding-skew, liquidation-cascade, mean-reversion, momentum-burst,
    spread-reversion, volatility-gap        confidence(PART_ID, regime_name)
    whale-flow-detector                     confidence(PART_ID, direction)
    universal-symbol-sweeper                confidence(PART_ID, condition.condition_id)
    sentiment-shift-detector                confidence(PART_ID, LEADS) and (PART_ID, CONTRARIAN)

`SignalCalibrator._estimator_for(detector, regime)` names its second slot
`regime`, and three of the nine put something else in it. That is not a defect —
a sweeper whose whole design is one record per watch condition is right to
segment that way, and a sentiment detector that compares its leading record
against its contrarian one could not work otherwise. But it means the outcome
cannot be routed by regime, and it means the key is currently implied by
whichever string each detector happens to pass rather than stated anywhere.

Naming it on the candidate fixes both. The claim states the key it is to be
scored against, the label carries it back unchanged, and the nine readers are the
same line of code. Under T-6 this is growth by making the data say more, not by
making a part cleverer; under Rule 7 it is a name that states what the thing is,
where today the second element of a tuple means three different things.

### Why the key also separates the two kinds of label

Two parts produce `training-label`, and only one of them is judging a signal:

    signal-outcome-labeller   from a candidate and the prices that followed
    label-builder             from a closed trade, after costs

Both set `THE_SETUP_WAS_RIGHT`, so a detector filtering on the component alone
would train its calibrator on trade outcomes too — and the calibrator's own
docstring forbids exactly that: *"whether the expected move happened within the
horizon — not whether a trade made money, which depends on sizing, stops and
timing that this detector had no part in."* A trade that was sized badly, entered
late or stopped early is not evidence about the setup.

`label-builder` has no candidate and so leaves `calibration_key` empty. A
detector takes only labels that carry one. The filter says what it means rather
than relying on `claimed_at_ns` being non-zero, which is true today and is not
a rule anyone wrote down.

## What will actually learn

Honestly: **one detector, today.**

    spread-reversion-detector    1,951 candidates -- every trading idea in the system
    the other eight                  0 candidates ever

Four of the eight are dark for want of a feed that does not exist on this box —
`volatility-gap` needs an implied surface, `whale-flow` needs on-chain transfers,
`funding-skew` needs a funding stream, `sentiment-shift` needs sentiment
readings. Wiring them costs nothing and they begin learning the day their feed
arrives, which is the point of wiring all nine rather than the one that works.

`momentum-burst`, `mean-reversion` and `liquidation-cascade` are dark on their
own thresholds, and two of those three are downstream of other entries in the
same backlog this proposal comes from — `liquidation-cascade-detector` reports
`no_cluster_in_reach` 485,199 times because `liquidation-cluster-mapper` has
`clusters_produced: 0`, which is `observe_liquidation` and `observe_open_interest`
uncalled, the same defect one part upstream.

So the measurable claim this proposal should be judged against is narrow and
checkable:

    before   detector_hit_rate missing on 100% of every vector ever built
    after    present on vectors from spread-reversion-detector once the
             calibrator has minimum_observations settled claims, and
             outcomes_learned climbing on that detector rather than sitting at 0

Not "the bot trades more". Whether a measured hit rate of 0.714 raises or lowers
what the model believes is the model's business, and a proposal that promised
more trades would be promising the answer rather than the evidence.

## Not in this change

**The 81 other unreachable evidence methods.** `check_learning_is_reachable.py`
found 82 across the 328 parts, every one of them cross-checked against the live
spine and every one sitting on a part whose matching counters read zero. This is
the first entry in that backlog and deliberately only the first: the rest include
`funding-rate-forecaster.observe_premium`, `liquidation-cluster-mapper.observe_liquidation`,
`slippage-learner.observe_unfilled`, `model-registry.record_outcome` and all four
of `control-recorder`'s recorders, and each needs its own reasoning about where
the evidence comes from.

**Whether these detectors should be raising candidates at all.** Eight of nine
have never raised one. That is a separate question from whether the ninth can
learn, and answering it inside this change would bundle a threshold argument into
a wiring fix.

**The conviction floor.** `bull-opinion-composer` stood down 1,690 times against
a floor of 0.686, which is a plan break-even implying a reward-to-risk near
0.46:1. A calibrated detector confidence moves conviction; it does not move the
floor. The floor is `stop-target-placer`'s business and belongs in its own
proposal.
