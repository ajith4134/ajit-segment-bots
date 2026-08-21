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

**What the danger-word pass actually covered, and what it missed.** Every
row-1 and row-2 candidate's role text was grepped against a list of words that
would signal an active, real-time invariant rather than a passive record:
`lock`, `reserve`, `instant`, `immediate`, `exclusive`, `concurrent`, `race`,
`invariant`, `guarantee`, `must`, `never`, `idempotent`, `double`, `zero the`,
`refuse`, `halt`, `kill`, `starve`, `atomic`, `sequence`, `hash chain`,
`corrupt`, `consistent`, `spent`, `one at a time`. Fourteen matches came back
across the whole registry (three of them — `skill-scorer`,
`skill-version-keeper`, `skill-provenance-stamper` — are the substring `kill`
inside the word `skill`, not a real hit). Of the eleven real matches, two
(`fund-lock-ledger`, `resource-reservation-ledger`) are the forced exceptions
above. Of the remaining nine, seven were judged correctly on the first pass and
are addressed below under "kept throttleable on purpose"; **two —
`leverage-selector` (`never`) and `tail-trailing-exit-planner` (`never`) — were
judged safe on the first pass and were wrong.** Both make a live decision in
the trade path, not an after-the-fact record, and the grep surfacing the word
`never` in their role text was exactly the signal that should have caught them;
it was read and dismissed instead of applied. See "Three corrections after an
independent audit" below — this is not a hypothetical, it is what actually
happened and was caught on review, and the fix is committed as a second,
separate blueprint edit rather than folded silently into this one.

**What the pass could not have caught at all.** `forecast-distribution-gate`
does not contain any of the words above — its role text is "flag a forecast
whose input features sit far from anything the model trained on", which reads
as ordinary model-quality language, not as guarding a live decision. A
keyword pass over role text cannot catch a danger that isn't spelled out in the
role text; it was found only by an independent full read of all 101
throttleable parts, not by any grep this document ran. That is a real limit of
the method used here, stated plainly rather than papered over: a keyword scan
finds the words it is told to look for, and a part whose risk lives in what its
output is used for rather than in how its own role sentence is phrased will
slip past it. The three corrections below close every instance that review
found; they are not proof no others remain.

One deliberate omission: the token `watch` was left out of every vocabulary
entirely. `margin-liquidation-watch` "zero[s] the limit when balance plus
unrealised PnL nears the maintenance margin" — an active risk control, not a
report — while `stale-board-watch` and `clock-skew-monitor` are genuinely
report-shaped. Rather than special-case every `watch`-named part individually,
the whole token stays out of the vocabulary, and every `watch`-named part falls
to the catch-all. That is the conservative direction to fall in.

## The rule for the next correction: live decision, or after the fact

The audit that found the two misjudgements above and the one the keyword pass
could not have found reduces to one test, and it is the test to apply the next
time a part's default is reviewed rather than re-deriving one from scratch:

**A part making a live decision in the trade path is not throttleable. A part
recording or auditing something that already happened is.**

"Live decision in the trade path" means: the part's output changes what a
later part does to size, place, hold, or protect a position, and a stale input
changes that output's *content* rather than only its arrival time — exactly
§6(a)'s test, restated at the level of a single part's job rather than its
name. "Recording or auditing after the fact" means: the part's job is to keep,
score, or check something that has already been decided elsewhere; running it
a tick later changes when the record lands, never what actually happened in
the trade path it is describing.

## Three corrections after an independent audit

An independent audit of all 101 parts this edit marked throttleable — not a
keyword grep, a full read of each one's role against the rule above — found
three that fail it. `dashboard/blueprint_edits/apply_2026-08-20_correct_live_decision_parts.py`
overwrites their `rate_risk` and `skipped_tick_effect` to the restrictive pair
(`resource_class` is untouched — this is a throttle-safety correction, not an
allocation one):

- **`leverage-selector`** — "choose leverage per trade from volatility plus
  funding, never above the user's ceiling". Stale volatility sizes leverage
  against out-of-date risk: that changes the answer, not merely when it
  arrives. It is the recursive-indicator failure this document already uses
  as its worked example (§ "Why the pair is not redundant"), applied to this
  part's own inputs rather than to an indicator's.
- **`tail-trailing-exit-planner`** — "trail a stop below the last higher low,
  tightening as the move ages". A stop left un-tightened through a fast move
  is a corrupted stop, not a late one.
- **`forecast-distribution-gate`** — "flag a forecast whose input features sit
  far from anything the model trained on". Throttling a safety gate widens the
  window an out-of-distribution forecast passes through unchecked — the gate's
  whole job is to not let that through, on time, and a slower gate is exactly
  the failure it exists to prevent.

Unlike `apply_2026-08-20_part_declarations.py`, which sets a field only if
absent, the correction script **overwrites** these three ids unconditionally.
That is the correct behaviour here, not a departure from the idempotence rule
stated below: this is precisely the "one-line hand edit... as each part is
actually [reviewed]" the next section describes as the intended process for
correcting a default, done as a small, named, idempotent script instead of an
untracked edit to `docs/features.json` so the change is reviewable and
re-runnable rather than silent.

## Four parts examined and kept throttleable on purpose

These four matched the same shapes the misjudged pair did, were checked
against the live-decision-versus-after-the-fact rule, and stayed as declared —
named here so the boundary reads as drawn on purpose rather than assumed:

- **`control-recorder`** — journals gate flips, policy rulings and
  self-modifications after they happen; nothing reads it to decide what to do
  next in the trade path.
- **`model-registry`** — keeps trained model versions with their gate
  verdicts; a version already exists and is already gated before this part
  records it.
- **`abstention-coverage-auditor`** — measures realised abstention coverage
  against a promise already made; the promise, not this measurement, governs
  the live decision.
- **`stop-placement-auditor`** — judges, after a trade has closed, whether its
  stop was too tight, too wide, or right; the stop itself was already placed
  and already hit by the time this part runs.

## This is a starting position, not a measurement

**Every one of these 321 defaults is an argued guess, not a probe result.**
Nothing here was run, timed, or profiled; the values are set from what a part's
name and role say about its shape, checked by hand against the failure modes
that would be dangerous to get wrong in the throttleable direction. Rule 8
applies to this document as much as to any board: a default is a position taken
so the checker has something to enforce, and it is corrected **part by part, as
each part is actually built and reviewed or measured** — not batch-revised, and
never re-derived from a bigger table. When a part's real behaviour disagrees
with its default, the fix is a one-line correction to that part's `rate_risk`
and `skipped_tick_effect` (or `resource_class`, if that is what turned out
wrong), delivered as a small, named, idempotent script the same shape as
`apply_2026-08-20_correct_live_decision_parts.py` above — never a rerun of the
classifier, and never a silent hand edit to `docs/features.json` outside of
one. `apply_2026-08-20_part_declarations.py` itself will never overwrite a
field once set, on any part, forever; a correction script is a second,
separate, explicit statement about specific ids, not a re-defaulting.

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
