# Live excursion and horizon profiling

**Proposed by Claude 2026-08-23. Agreed by the user the same day**, after the
alternatives were put to them: measure the profiles from live prices (this), wire
the volatility forecaster as a weaker proxy, or seed the profiles from the tape.
The third was recommended against and not chosen — it would put replayed history
inside a live decision, which is what RL-071 was written against.

## The problem, measured

The bull bot's conviction model became trained for the first time on 2026-08-23 at
05:35, on the live spine: **102 labelled outcomes, 52 right and 50 wrong**, both
classes, past the hundred `bull_minimum_training_observations` asks for. Its
conviction is now a measurement.

It still forms no opinion. In the first 27 minutes of that run the detectors raised
**9 900 entry candidates and the arbiter formed zero intents**.

The cold start moved one layer along. `bull-exit-plan-proposer` refuses with
`NO_EXCURSION_PROFILE` unless it holds a fitted `excursion-profile`, and with
`NO_HORIZON` unless it holds a fitted `horizon-profile`. `bull-opinion-composer`
refuses with `NO_EXIT_PLAN` without a complete plan. So:

    candidate → filter → features → conviction ✓ → exit plan ✗ → opinion → intent

The two profiles come from `excursion-profiler` and `holding-horizon-profiler`,
both in `closed-trade-decoding`, and both consume `closed-trade`. A closed trade
needs an open trade, which needs an intent, which needs an exit plan, which needs
the profiles. That is the same cycle `signal-outcome-labeller` was created to
break, one layer along, and it has the same answer.

## What is proposed

**A detector's claim already contains both measurements, and the labeller already
takes them.** `OpenClaim` tracks `best_favourable_fraction` and
`worst_adverse_fraction` on every tick, and `TrainingLabel` already carries
`seconds_to_resolve`. What has been missing is not a measurement — it is that the
measurement was thrown away when the claim settled.

So:

1. **`TrainingLabel` carries what the claim measured.** Three fields added:
   `direction`, `best_favourable_fraction`, `worst_adverse_fraction`. Nothing new
   is computed; the label reports numbers the claim already held.

2. **`signal-excursion-profiler`** (new, learning-loop) consumes `training-label`
   and produces `excursion-profile`. Per venue, symbol and side, over a rolling
   window of settled claims: the adverse-excursion quantile — how far price went
   against a claim that then came right — and the favourable quantiles, which is
   where targets belong.

3. **`signal-horizon-profiler`** (new, learning-loop) consumes `training-label`
   and produces `horizon-profile`. Per detector: the median seconds a claim took
   to resolve.

**Why they read labels rather than the feed.** Tracking claims is the labeller's
hot loop, and it is deliberately indexed by symbol because at the 5 000-claim bound
and 285 trades a second a linear scan is over a million comparisons a second. Three
parts each holding the same claims and touching every one on every trade would be
three times that, to compute the same numbers three times. Reading the label is one
message per settled claim.

**Why this is not making a part cleverer (T-6).** The labeller publishes the same
type to the same consumer; two of its fields stop being discarded. The new
behaviour is two new parts, which is exactly how T-6 says to grow.

**Why the numbers mean what the proposer thinks they mean.** The proposer's
`adverse_excursion` is documented as "the excursion a winning trade normally
survives". Here it is the excursion a *claim that came right* survived. Those are
not the same thing and the difference is stated rather than papered over: a claim
is not a trade, it has no entry slippage, no fees and no size. What transfers is
the shape of the symbol's movement inside the horizon a detector named, which is
what a stop distance is actually about. When real trades exist, `excursion-profiler`
measures the same type from them, and the two producers can be compared — which is
the point of leaving both on the same data type rather than inventing a second one.

## What this deliberately does not do

- **It does not replace the closed-trade profilers.** They measure the same types
  from real round trips and are strictly better evidence when they exist. This is
  what the system uses before it has any.
- **It does not seed from the tape.** Every claim it profiles was raised by a
  detector on live prices and settled by live prices (RL-071).
- **It does not make the profile look fitted before it is.** Both parts report
  `is_fitted` false until their minimum number of settled claims exists, and the
  proposer already refuses an unfitted profile.

## Two defects on the same edge, found while doing this

Both are recorded here rather than fixed, because both belong to parts that are
off, unwired, and outside this phase. Neither may be switched on before they are
fixed (RL-067: what is built matches what is declared).

- **`holding-horizon-profiler` publishes a shape no consumer of `horizon-profile`
  can read.** It publishes `runtime.trade_decoding_types.HorizonProfile`, whose
  fields are `setup`, `payoff_by_horizon`, `decays_after_seconds` — a measurement
  of *where holding longer stops paying*. The bull and bear proposers read
  `detector` and `median_seconds` — *how long a trade takes to resolve*. Two
  different measurements wearing one name and one data type. The split belongs in
  a blueprint edit of its own: `edge-decay-profile` for the decoding measurement,
  which is what `expectancy-decomposer` and `instruction-writer` actually want.

- **`excursion-profiler` publishes `PeakExcursionProfile`**, whose fields are
  `adverse_quantile` and `favourable_quantile`, where its consumers read
  `adverse_excursion`, `favourable_quantiles` and `trades_observed`. It also
  publishes a single object where the bus takes an iterable — the same defect that
  killed `peak-excursion-tracker` and `position-close-detector` on 2026-08-23 —
  and reaches into its own `_counts` from outside the object.

Both are the failure mode this project keeps paying for: a part that is correct on
its own and speaks a shape its neighbour cannot read. They are invisible until the
part is forked, which is why `test_the_closing_chain_runs_as_processes` exists.

## Settings

Each carries its provenance (RL-061). None is a tuning knob picked to make a
number come out.

| Setting | What it decides |
|---|---|
| `signal_excursion_window` | how many settled claims per symbol and side the quantiles are estimated over |
| `signal_excursion_minimum_claims` | how many before the profile reports itself fitted |
| `signal_excursion_adverse_quantile` | which adverse quantile the stop is placed beyond |
| `signal_horizon_window` | how many settled claims per detector the median is taken over |
| `signal_horizon_minimum_claims` | how many before the horizon reports itself fitted |

The favourable quantiles are not a new setting: they are the ones
`bull_exit_target_quantiles` already names, because a profile that priced
quantiles nobody asked for would be measuring one thing and being read for another.

## How it is verified

- The quantiles and the median are estimated on real captured trades (RL-063),
  through the labeller, so the claims are ones detectors actually raised.
- The two new parts run as forked processes in the same integration test that
  proves the closing chain, because a `start_part` that binds the wrong field
  produces silence and silence looks like a quiet market.
- **The measurement that settles it is the live run**: an `excursion-profile` and
  a `horizon-profile` that report themselves fitted, followed by the first
  `bull-exit-plan`, the first `directional-opinion` and the first `trade-intent`
  the system has ever formed from its own learning.
