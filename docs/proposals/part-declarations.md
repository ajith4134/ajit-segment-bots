# Every part declares its resource class and its two throttle facts

Proposed by Claude 2026-08-20. Applied as `origin` unchanged (this edit adds fields,
it does not touch `origin`) by
`dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py`; open to veto.

This closes sections 6 and 7 of
`docs/superpowers/specs/2026-08-20-part-runtime-design.md`: every one of the 321
parts in `docs/features.json` gets three new fields, and the contract checker
refuses a part missing any of them or carrying a value outside the declared
vocabulary (`runtime/part_declaration.py`).

## The three fields

- **`resource_class`** — how the governor should allocate to this part (§7):
  `io-bound` (shared pool, generous concurrency — blocked in `epoll_wait`, costs
  nothing while idle), `compute-bound` (pinned cores, BLAS threads = 1, because the
  governor owns parallelism), or `bandwidth-bound` (capped at 2–4 threads
  regardless of idle cores — one memory controller, one NUMA node, and two are
  never co-scheduled because they divide a fixed pipe rather than adding
  throughput).
- **`rate_risk`** — §6(a): does a lower tick rate change the *number* a part
  produces (`changes-the-answer`), or only *when* that number arrives
  (`latency-only`)?
- **`skipped_tick_effect`** — §6(b): does a skipped tick *corrupt* a monotone
  invariant the part maintains (`corrupts`), or merely *delay* it (`delays`)?

## Why the pair is not redundant

`may_enter_rate_ladder` in `runtime/part_declaration.py` admits a part to a rate
ladder only when **both** hold: `rate_risk` is `latency-only` **and**
`skipped_tick_effect` is `delays`. A part can fail either test independently:

- A recursive (IIR) indicator's *value* is biased by a shorter window — that is
  `rate_risk = changes-the-answer` even though nothing about it is a monotone
  invariant in the sequence-completeness sense.
- Order-book reconstruction is `rate_risk = latency-only` in the narrow sense that
  a slower feed does not itself bias any one snapshot — but a skipped tick leaves
  the book unreconciled, which is exactly the invariant §6(b) is asking about:
  `skipped_tick_effect = corrupts`.

Two parts can share a `rate_risk` value and still disagree on
`skipped_tick_effect`, and the reverse. Collapsing them into one field would
force one part to average two independent risks into a single "sort of safe"
verdict; keeping them separate lets the checker refuse a part whenever *either*
is missing or *either* says no.

## How the 321 defaults were chosen

Section 6 describes throttle safety in terms of what a part *is* — a feed
reader, an indicator, a model, a ledger — not in terms of which blueprint block
it happens to sit in. A blueprint category is frequently a mix: `bull-bot`
contains a feature builder, a conviction model, a calibrator and a learner in
one block, and those four are not equally safe to throttle. So the edit script
classifies **per part**, from the part's own `id`, not from its category.

Each `id` is a hyphenated compound (`kronos-forecaster`, `fund-lock-ledger`).
Split on `-`, the tokens are checked against four ordered vocabularies, most
specific and highest-signal first; the first vocabulary a part's tokens
intersect wins. A part matching none of the four gets the catch-all.

| Order | Shape | Token vocabulary | `resource_class` | `rate_risk` | `skipped_tick_effect` | Parts matched |
|---|---|---|---|---|---|---|
| 1 | ledger, journal, provenance, reporting | `ledger` `journal` `provenance` `recorder` `registry` `keeper` `archive` `auditor` `checker` `store` `cache` `publisher` `versioner` `accountant` `verifier` `monitor` | `io-bound` | `latency-only` | `delays` | 43 |
| 2 | model, scorer, search, learning | `model` `scorer` `scorekeeper` `learner` `learning` `forecast` `forecaster` `classifier` `regressor` `calibrator` `search` `miner` `optimizer` `ensembler` `critic` `estimator` `selector` `ranker` `decoder` `reflector` `planner` `picker` | `compute-bound` | `latency-only` | `delays` | 58 |
| 3 | indicator, feature, rolling-window | `indicator` `feature` `window` `tracker` `gauge` `meter` `profiler` `aggregator` `consolidator` | `bandwidth-bound` | `changes-the-answer` | `corrupts` | 22 |
| 4 | feed reader, socket loop, REST poller, venue client | `reader` `feed` `venue` `poller` `fetcher` `socket` `api` `caller` `rotator` `resubmitter` | `io-bound` | `changes-the-answer` | `corrupts` | 35 |
| — | everything else (catch-all) | *no match* | `compute-bound` | `changes-the-answer` | `corrupts` | 163 |

`43 + 58 + 22 + 35 + 163 = 321`.

**Why this order.** A part whose own name says `ledger` or `journal` is
unambiguous about what it keeps, so that vocabulary is checked first. `model` /
`scorer` / `learner` words are the next most specific. `indicator` / `feature` /
`window` words come third. `reader` / `venue` / `feed` words come last among the
four — a part named `reader` that is really a scorer (there are none in this
registry, but the ordering is what would resolve one) should read as a scorer,
not as a feed client. Rows 1–2 are throttleable by construction (both facts are
the safe pair); rows 3–4 and the catch-all are not, so an ordering mistake
*between* row 3 and row 4 only misassigns `resource_class` — a governor
efficiency question, not a correctness one. An ordering mistake that pulled a
part *into* row 1 or row 2 when it should not be there is the one that matters,
and every row-1/row-2 assignment was checked by hand against its role text for
exactly that risk (see "Two forced exceptions" below).

**Two forced exceptions.** `fund-lock-ledger` and `resource-reservation-ledger`
both carry the `ledger` token and would otherwise land in row 1
(`io-bound` / `latency-only` / `delays`, throttleable). Both are wrong to
throttle: `fund-lock-ledger` "reserve[s] capital against an order the instant it
is sent, so two orders in one tick never spend the same balance" — delaying the
reservation *is* the double-spend it exists to prevent, not a delay of some
separate answer. `resource-reservation-ledger` "hold[s] a guaranteed CPU plus RAM
floor for parts that must never be starved" — delaying the reservation is the
starvation it exists to prevent. Both are active locks, not passive records; the
word `ledger` in this registry covers both shapes, and only the passive one
(`paid-spend-ledger`, a spend tally that is still accurate whenever it is next
read) is safe to throttle. The edit script forces both to the catch-all
regardless of their token match, with the reasoning above inline as a comment.

Every other row-1 and row-2 candidate was checked against its role text for the
same class of danger — words like `lock`, `reserve`, `instant`, `never`,
`starve`, `atomic`, `sequence`, `hash chain`, `double` — and none of the
remaining matches control a live trading invariant the way these two do; the
ones that came up (`journal-integrity-checker`, `funding-settlement-recorder`,
`lookahead-auditor`) are either idempotent by construction or audit an offline
replay, not a live position.

One deliberate omission: the token `watch` was left out of every vocabulary
entirely. `margin-liquidation-watch` "zero[s] the limit when balance plus
unrealised PnL nears the maintenance margin" — an active risk control, not a
report — while `stale-board-watch` and `clock-skew-monitor` are genuinely
report-shaped. Rather than special-case every `watch`-named part individually,
the whole token stays out of the vocabulary, and every `watch`-named part falls
to the catch-all. That is the conservative direction to fall in.

## This is a starting position, not a measurement

**Every one of these 321 defaults is an argued guess, not a probe result.**
Nothing here was run, timed, or profiled; the values are set from what a part's
name and role say about its shape, checked by hand against the failure modes
that would be dangerous to get wrong in the throttleable direction. Rule 8
applies to this document as much as to any board: a default is a position taken
so the checker has something to enforce, and it is corrected **part by part, as
each part is actually built and measured** — not batch-revised, and never
re-derived from a bigger table. When a part's real behaviour is measured and
disagrees with its default, the fix is a one-line hand edit to that part's three
fields in `docs/features.json`, which this script will never overwrite (see
below), not a rerun of this classifier.

**The catch-all (163 of 321 parts, more than half) is deliberately the most
restrictive combination available**: `compute-bound` / `changes-the-answer` /
`corrupts`. `may_enter_rate_ladder` refuses anything that is not
`latency-only` **and** `delays`, so every catch-all part is non-throttleable by
default. This is intentional and asymmetric on purpose. §6 says the two errors
are not symmetric: refusing to throttle something that could safely have been
throttled costs a part being evicted instead of slowed — visible, recoverable,
reported. Throttling something that could not costs a number that is wrong
while still looking healthy, and the failure mode of a silently biased
indicator is a trade placed on it. A large catch-all is therefore the *correct*
shape for a first pass across a design that has not yet measured any of it —
not a gap to be embarrassed about closing later, and not evidence the
classification under-covered the registry.

## No edge moves

This edit only adds the three fields above to each of the 321 features already
in `docs/features.json`. It does not read, write, or otherwise touch any
feature's `consumes` or `produces`, does not add or remove a feature, a category,
or a data type, and does not change any category's `peer_group`. R-01's derived
edges and R-03's peer-group check are both computed from `consumes` /
`produces` / `peer_group`, so neither can have moved; `dashboard/build_wiring_explorer.py`
and its verifier (Step 7 of the implementing task) are the check that proves it,
by diffing the wiring output before and after this edit lands.
