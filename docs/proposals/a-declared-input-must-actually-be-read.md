# A declared input must actually be read

**Proposed 2026-09-06**, walking `opportunity-scanner` — feature 5 of 29 under
the audit temporary goal. This is item 1 of that goal ("is data actually
arriving on each `consumes` type — measured, not inferred from the contract
declaration") turned into a check that runs, and RL-067 ("a part's real consumes
and produces equal what the blueprint declares") enforced in the one direction
nothing was enforcing it.

## What was found

`expiry-day-zero-to-hero-detector` declares `broker-price-frame` and never binds
a reader for it. The type appears exactly once in the file — in the
`PART_DECLARATION` tuple — and nowhere else. `broker-price-level-sampler` was
publishing 5,868 of them at the time, so this reads on the audit board as a wire
whose producer is healthy and whose consumer is receiving nothing, which is
indistinguishable from a real delivery fault until someone opens the file.

A scan of every launchable part found **six** parts in that state, plus fourteen
more that bind their readers dynamically (`context.bus.reader(data_type)` inside
a loop) and therefore cannot be checked statically at all.

## Why nothing caught it

The project already has three checkers, and none of them can see this:

| | what it proves |
|---|---|
| `check_contracts.py` | the blueprint is internally coherent (R-01) — it never opens a part's source |
| `check_payload_reads.py` | a field a part reads is one some producer of that type carries |
| `check_part_calls.py` | a call a part makes is one its own object can answer |

The first checks the declaration against itself. The second and third check the
code against the declaration for types the code *does* read. Nothing checked the
declaration against the code for a type the code reads **not at all**, so a
declared input could sit unbound for as long as nobody looked.

That gap matters more than a stray tuple entry, because the declaration is what
the whole wiring is computed from. `docs/features.json` derives every edge from
consumes/produces (R-01), so a declared-and-unread input is a wire on every
diagram, a row in the wiring explorer, and a `NOT CARRYING` line on the audit
board — a fault reported against a producer that is doing its job perfectly.

## The five resolved here

Each was resolved by asking what the part actually needs, never by deleting the
line to quiet the checker:

- **`expiry-day-zero-to-hero-detector` — `broker-price-frame`.** It judges how
  far out of the money a strike is from the **delta** Upstox publishes
  (`zero_to_hero_maximum_abs_delta`, its own `not_far_enough_otm` counter), not
  from the underlying's spot. It has never needed a price frame.
- **`bull-feature-builder` and `bear-feature-builder` — `symbol-universe`.**
  Both build one vector per candidate (`build(candidate)`, keyed by
  `(venue_id, symbol)`) and learn which symbols matter from
  `bull-side-candidate` / `bear-side-candidate`. The universe is what the
  *scanner* sweeps; a feature builder tracks what it was handed.
- **`venue-trade-stream-reader` and `venue-quote-stream-reader` —
  `venue-standing`.** Both are crypto venue parts, both off, and both being
  retired by the Indian conversion — their live equivalents are the
  `broker-*-bridge` family. A ban signal genuinely should stop a stream reader,
  but the part that would produce it (`ban-signal-detector`) is off too, and the
  Indian-market equivalent of that whole concern is named as unbuilt in
  `docs/feature-audit.md`'s feature-1 section. A declaration that lies is worse
  than one that is absent.

## The one left open

**`opinion-arbiter` — `bot-maturity`** is not a stray line and is not resolved
here. The arbiter decides which bot's opinion wins a contested symbol, and how
proven a bot is, is exactly the sort of thing an arbiter should weigh — the
declaration records a real design intent rather than an accident.

It is also dead at the source: `edge-graduation-gate`, the only producer of
`bot-maturity`, has published nothing, and four other parts consume it
(`autonomy-boundary`, `live-switch-guard`, `exploration-pair-opener`,
`opinion-conflict-resolver`). Binding it in the arbiter alone would change how
trades are arbitrated while still receiving nothing.

Deleting the declaration would erase a design decision; binding it would change
trading behaviour. Both are the operator's call, so it is recorded rather than
guessed, and the checker is not wired into the pre-commit hook until it is
answered.

## The checker

`dashboard/check_declared_inputs.py`, in the shape of the other three:

- reads `docs/features.json` and every part module that carries a `start_part`;
- compares the `consumes` tuple in the part's own `PART_DECLARATION` against
  every literal string handed to `context.bus.reader(...)` in that module;
- reports a **defect** only when the part binds readers exclusively by literal
  and still misses a declared type;
- reports a part that binds any reader dynamically as **not statically
  checkable**, counted and named, never silently passed. Fourteen parts are in
  that state today, and calling them clean would be the same kind of lie this
  check exists to catch (Rule 8: absence of evidence renders as its own state).

It does not check the reverse direction — a reader bound for a type the part
does not declare — because `check_payload_reads.py` already fails on any read of
a type the blueprint does not carry for that part.
