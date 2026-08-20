# The three bots inside a segment bot: what feeds them, and what is inside each

Proposed by Claude 2026-08-20; the three open questions were answered the same day as RL-046 (scanner stays blind, weight lives in the bot), RL-047 (tailgater follows own winner) and RL-048 (bull and bear separate, same skeleton). Applied by `dashboard/blueprint_edits/apply_2026-08-20_harvest_and_three_bots.py`.
Binding rulings: RL-009 (teacher watches the whole class), RL-011 (bull, bear and
profit tailgating are each a complete bot with its own deep architecture and data),
RL-014 (watch every symbol), RL-023 (not bull/bear/arbiter), RL-026 (real AI, not
rules), RL-043 (intraday 1m/5m/15m/30m).

## 1. What is wrong today

`docs/features.json` has `bull-bot`, `bear-bot`, `profit-tailgater` as **one part
each** — four inputs, one output. That contradicts RL-011 and T-1: every other
foundation block was opened into a mini project with 5-13 parts; these three were
left as single boxes. The "deep architecture" the ruling asks for does not exist in
the blueprint yet.

## 2. The connection: scanner → bots

### The user's idea
The universal scanner watches every symbol, picks the ones whose environment is ripe,
and points them at the three bots.

### Verdict: right, with three corrections

**Correct for bull and bear.** One scanner per segment, every symbol, every tick.
Three scanners (one per bot) would sweep 500 futures symbols × 4 time-frames three
times over on one machine — the queueing the resource governor exists to prevent.
Conditions are data (`watch-condition`); the sweep is one part. Keep it.

**Correction 1 — the candidate carries the setup, never a side.** Today
`entry-candidate` is "a symbol and the moment". It must also name *which* condition
fired and with what reading (e.g. `momentum-burst`, +4.1% in 3 min, 6× volume). Bull
and bear both receive every candidate; deciding the side is the bots' job (RL-011).
The scanner saying "short this" would put direction back into the scanner, which is
what RL-011 forbids.

**Correction 2 — each bot declares its appetite as data.** A bot cannot talk to the
scanner (T-4). So each bot produces a `setup-appetite`: which setup classes it wants
to see, and on which time-frames, given its current scorecard. The scanner consumes
it and routes. A bear bot that has learned it loses on funding-skew setups stops
receiving them — learned, not hard-coded. The appetite is written by the bot's own
scorecard, so it is real learning (RL-026), and the "teacher" in RL-009 still holds
every student: the scanner still watches every symbol, it only stops handing one
kind of exam to a bot that keeps failing it.

**Correction 3 — the profit tailgater is not fed by reversal setups.** Bull and bear
hunt *turns* (pullback, burst-fade, squeeze). The tailgater hunts *continuation*:
something already working. Its inputs are three, and only one is the scanner:

| source | data | what it means |
|---|---|---|
| scanner | `entry-candidate` tagged `continuation` | a sustained move the sweeper flagged, not a reversal |
| our own book | `position` in profit | the bull or bear leg of an exploration pair that is winning — tailgate the winner, that is what the pair was for |
| outside | `external-position` + `copy-score` | a tracked wallet or leaderboard trader whose verified record says follow |

So the tailgater is the one bot that reads the book. That is not a leak across
segments: it reads its own segment's positions, which it already does today.

### Flow, after the corrections

```
market-data ─┐
symbol-universe ─┤                ┌─ setup-appetite ◄── bull bot
watch-condition ─┼─► sweeper ─► entry-candidate(setup, reading) ─► bull bot ─► directional-opinion ─┐
setup-appetite ──┘   (one per      │                                                                  ├─► opinion-arbiter ─► trade-intent
                     segment)      ├─► bear bot ─► directional-opinion ──────────────────────────────┤
                                   └─► [continuation only] ─► tailgater ─► directional-opinion ─────┘
position (in profit) ──────────────────────────────────────────► tailgater
external-position + copy-score ────────────────────────────────► tailgater
```

Nothing here changes the arbiter, the scorekeeper or the ledger. Three data types are
added (`setup-appetite`, and two fields inside `entry-candidate`); no part switches
another; every new part names only data.

## 3. Inside the bull bot — and, mirrored, the bear bot (T-1: same template)

Bull and bear are **one part template instantiated twice with the side flipped**.
That is what T-1 demands and it is also why bear is not "bull with a minus sign":
the features differ (a dip into support is not a rally into resistance; funding
negative means something different to each), but the *shape* is the same. Every
part below exists once per side.

| part | consumes | produces | responsibility |
|---|---|---|---|
| `setup-filter` | entry-candidate, setup-appetite | side-candidate | keep only candidates whose setup this side can trade |
| `side-feature-builder` | side-candidate, market-data, order-book-snapshot, symbol-profile, funding-forecast | feature-vector | the side's own features: dip depth vs support, buy/sell imbalance, distance from VWAP, funding sign, burst age, volume ratio vs the symbol's own hour-norm |
| `outlier-rejector` | feature-vector | feature-vector (or nothing) | refuse to opine when the feature vector is far from anything the model has seen (FreqAI's dissimilarity index; harvested 2026-08-20) |
| `conviction-model` | feature-vector, price-forecast, kline-window | raw-conviction | the real AI: a learned model (gradient-boosted on the side's outcomes, Kronos forecast as an input feature) giving P(move in my direction ≥ cost within horizon) |
| `conviction-calibrator` | raw-conviction, bot-scorecard | calibrated-conviction | map the model's number onto the bot's measured hit-rate so 0.7 means 70% for *this* bot |
| `entry-timer` | side-candidate, market-data, calibrated-conviction | entry-timing | now, or wait for the pullback the playbook says comes; with a give-up time |
| `exit-plan-proposer` | side-candidate, market-data, symbol-profile, calibrated-conviction | exit-plan | target, stop, and time-stop for this side, from ATR and structure — proposed, the risk gate still decides |
| `opinion-composer` | calibrated-conviction, entry-timing, exit-plan | directional-opinion | pack the opinion: side, conviction, timing, plan, feature snapshot for the critic |
| `appetite-writer` | bot-scorecard, instruction-performance | setup-appetite | rewrite what this bot asks the scanner for, from what it has actually been right about |
| `position-invalidation-watcher` | position, market-data, feature-vector | directional-opinion (flat) | for this side's open positions: the reason to be in has gone — say so |

Ten parts, mirrored = twenty. The `exit-opinion-bot` that exists today becomes the
two `position-invalidation-watcher`s, one per side, because only the side that
opened a position knows why it was opened.

Where the "real AI" is: `conviction-model` and `conviction-calibrator` are learned;
`appetite-writer` is learned; `outlier-rejector` is the honesty check on the learned
part. `setup-filter`, `entry-timer` and `exit-plan-proposer` start as playbook rules
and are retired by learned versions the way RL-025 says — rule brains now, learned
later, each as its own part so the swap is T-6 (add a part), not a rewrite.

## 4. Inside the profit tailgater

Different job, different parts, same template shape.

| part | consumes | produces | responsibility |
|---|---|---|---|
| `mover-qualifier` | entry-candidate (continuation), market-data, symbol-profile | follow-candidate | is this move sustained (higher lows on 1m/5m, volume holding) rather than a spike already fading |
| `winner-selector` | position, market-data | follow-candidate | our own open leg that is in profit and still moving — the exploration pair's winner |
| `copy-selector` | external-position, copy-score | follow-candidate | a tracked trader's position that the copy score says follow |
| `move-remaining-estimator` | follow-candidate, market-data, price-forecast | move-remaining | how much of the move is likely left vs how much is already gone — the chasing question |
| `crowding-detector` | follow-candidate, order-book-snapshot, funding-forecast, sentiment-reading | crowding-reading | is everyone already in (funding stretched, book one-sided, sentiment extreme) — then the tail is the exit liquidity |
| `follow-conviction-model` | follow-candidate, move-remaining, crowding-reading, bot-scorecard | calibrated-conviction | learned: P(continuation ≥ cost) given remaining move and crowding |
| `trailing-exit-planner` | follow-candidate, market-data, symbol-profile | exit-plan | a tailgater never has a target: it trails — stop below the last higher low, tightening as the move ages |
| `opinion-composer` | calibrated-conviction, exit-plan | directional-opinion | same as the others |
| `appetite-writer` | bot-scorecard | setup-appetite | ask the scanner for continuation classes it is actually right about |

Nine parts. Note what it does **not** have: it never proposes a reversal, never
opens against the two others, and its stop is structural, not a fixed distance.

## 5. What the arbiter gains

The arbiter already consumes `directional-opinion`. With the parts above, every
opinion carries a *calibrated* conviction, so the arbiter compares like with like
across three bots with different hit-rates — that is the thing the current single-part
bots cannot give it. No change to the arbiter's contract.

## 6. Counts, if accepted

- 3 single parts retired (`bull-bot`, `bear-bot`, `profit-tailgater`), 1 merged
  (`exit-opinion-bot` → two invalidation watchers).
- 29 parts added across two new blocks: `directional-bot` (instantiated ×2, bull and
  bear) and `profit-tailgating-bot`. 22 blocks → 24.
- New data types: `setup-appetite`, `side-candidate`, `feature-vector`,
  `raw-conviction`, `calibrated-conviction`, `entry-timing`, `exit-plan`,
  `follow-candidate`, `move-remaining`, `crowding-reading`. `entry-candidate`
  gains `setup` and `reading` fields.

## 7. Open questions for the user

1. `setup-appetite` — a bot asking the scanner for fewer exam types: is that allowed
   under RL-009, or must every bot see every candidate always (then the appetite
   becomes a weight the bot applies itself, and the scanner stays blind)?
2. Tailgater reading our own winning leg: intended, or should it only follow
   outside traders and continuation setups?
3. Bull and bear as one template ×2 (T-1) versus two separately-written bots —
   RL-011 says "its own deep architecture"; I read that as own features and own
   model, not a different shape. Confirm or correct.
