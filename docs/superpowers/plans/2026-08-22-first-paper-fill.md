# The first paper fill — phase 3 plan

**Spec:** `docs/superpowers/specs/2026-08-22-part-wiring-design.md` §10, which fixed
the method: switch on the smallest set that carries a trade from the tape to a
simulated fill, and leave the rest genuinely off.

**What "done" means:** a fill that came from a real trade the tape recorded, made
by parts running as their own processes, wired only by the blueprint, recorded in
the ledger — and a board that shows the live path lit and everything else dark.

---

## Why this is not "run everything"

The transitive input closure of `paper-fill-simulator` is **306 of 321 parts**. That
is not a build order; it is the reason there is no build order. What makes a partial
run honest is the transistor: **a part with an empty inbox produces nothing, and a
part that is off costs nothing.** So the run switches on a decision spine and lets
every optional input stay empty.

Parts that then refuse to act are the finding, not the failure. A part that cannot
decide without an input nobody is producing has just named the next batch.

## The decision spine — 21 parts

Each must act for a fill to exist. Listed in the order data moves, with what it
must publish for the next one to have anything to read.

| # | Part | Publishes |
|---|---|---|
| 1 | `venue-trade-stream-reader` | `market-data` |
| 2 | `regime-classifier` | `market-regime` |
| 3 | `momentum-burst-detector` | `entry-candidate` |
| 4 | `bull-setup-filter` | `bull-side-candidate` |
| 5 | `bull-feature-builder` | `bull-feature-vector` |
| 6 | `bull-outlier-rejector` | `bull-feature-out-of-distribution-flag` |
| 7 | `bull-conviction-model` | `bull-raw-conviction` |
| 8 | `bull-conviction-calibrator` | `bull-calibrated-conviction` |
| 9 | `bull-entry-timer` | `bull-entry-timing` |
| 10 | `bull-exit-plan-proposer` | `bull-exit-plan` |
| 11 | `bull-opinion-composer` | `directional-opinion` |
| 12 | `opinion-arbiter` | `trade-intent` |
| 13 | `instrument-selector` | `instrument-choice` |
| 14 | `capital-allotment-reader` | `trade-capital-bounds`, `capital-allotment` |
| 15 | `position-sizer` | `sized-order` |
| 16 | `trade-capital-bounds-gate` | `bounded-order` |
| 17 | `order-idempotency-stamper` | `stamped-order` |
| 18 | `money-mode-reader` | `money-mode` |
| 19 | `order-destination-router` | `order-request` |
| 20 | `paper-fill-simulator` | `fill` |
| 21 | `paper-account-keeper`, `trade-lifecycle-recorder` | `account-balance`, `journal-entry` |

**Only the bull bot runs.** Bear and tailgater stay off: they are peer blocks
(R-03), and a first fill needs one opinion, not three. Their absence is what
`opinion-arbiter`'s sole-opinion penalty is for, and whether that penalty then
blocks every trade is one of the things this run finds out.

**Two of these are non-negotiable and are checked, not trusted:**
`money-mode-reader` must report paper, and `order-destination-router` must address
every order to the paper book. A run that reached a live venue would be the one
failure this phase cannot recover from (RL-005: paper first, without restriction).

## What each part costs to wire

One `start_part` — around twenty lines: build the engine from settings, bind each
`read_*` to the inboxes of the types the part declares it consumes, bind each
`publish_*` to the bus, call the part's existing `run_*`. The logic and the tests
already exist; none of them is touched.

**The real work is the numbers.** These 21 parts take roughly seventy constructor
arguments between them — window lengths, thresholds, fee rates, learning rates —
and RL-061 says each is either estimated from data or a named setting carrying its
provenance. Neither is free:

- **Where the tape can answer, it answers.** A window length is a number of
  observations, and the tape says what an observation costs in wall-clock time:
  285.3 messages a second across 62 symbols on 2026-08-22, about 4 a second for
  BTCUSDT. A window is chosen as a span of market time and converted, with the
  measurement cited.
- **Where it cannot, the note says so.** A fee rate is the venue's published
  schedule, not a measurement. A minimum reward-to-risk is a judgement. Those are
  written as judgements, with what would settle them, exactly as
  `io_stall_fraction` already is.

A number that cannot say which of those it is does not go in.

## Batches

Each batch ends green, committed, and with the wiring probe holding.

1. **Feed and regime** — 1, 2. Proves market-data crosses a process boundary from
   the part that is already recording the tape.
2. **Notice** — 3, 4. First `entry-candidate` from a real trade.
3. **The bull's opinion** — 5 through 11. The longest batch and the one with the
   learned components (RL-060).
4. **Intent and size** — 12 through 16.
5. **The order** — 17, 18, 19. Including the paper-only assertions above.
6. **The fill and the record** — 20, 21. The first paper fill.
7. **The board** — the run's own evidence: which parts were on, what each
   published, and the fill with its provenance.

## What this phase will not do

- No live venue, no keys, no order that can move money.
- No bear bot, no tailgater, no spot, no options.
- No governor deciding what runs: the spine's parts are switched on deliberately
  for this run. `switching-planner` deciding the on-set is later work, and pretending
  otherwise would be a governor that had not been measured.
- No claim that the trade was *good*. The first fill proves the circuit conducts.
  Whether the number it produced is worth anything is what the learning loops in
  phase 4 exist to answer.
