# Market data feed — phase 1 implementation plan

**Spec:** `docs/superpowers/specs/2026-08-21-market-data-feed-design.md` — binding.
Read it before any task here. This plan is its argument, not a second source of truth.

**Branch:** `phase-1-market-data-feed`, from `main` at `0bf7722`.

---

## The ordering principle

**Get bytes from a live venue onto disk as early as honestly possible.**

History accrues only in real time. Every hour before the tape starts is an hour
that cannot be captured later, and no other phase can recover it. So the task
order is not "simplest first" or "dependencies first" — it is *shortest path to a
running capture*, then breadth.

Tasks 1–7 exist to make the tape run. **From task 7 the tape is recording**, and
tasks 8–19 are built while it does. Nothing in tasks 1–7 is a shortcut toward
that: each is a real component of the finished system, because RL-058 forbids a
placeholder and RL-062 binds futures. Fast here means *fewer things before the
first real capture*, never *rougher things*.

## Who writes this

Claude writes the implementation; a fresh reviewer checks each finished piece
(Rule 1, point 7). That inverts phase 0's shape and changes this plan's form:
phase 0's plan embedded complete code for subagents to transcribe, which is why
it ran to 163 KB. Here the code is written once, in the file it belongs in. This
plan carries intent, order, and the constraints that are easy to forget — not a
copy of the source.

## Global constraints

Every task inherits these. Most are phase 0's, still binding.

- **Standard CPython 3.14.4.** No C compiler, no sudo, no `apt-get`. Every
  dependency must arrive as a prebuilt `cp314` wheel or it cannot be installed.
- **No numeric literals in decision code (RL-061).** Every venue limit, interval,
  threshold and count comes from a settings entry citing the spec's §1, or from a
  runtime measurement carrying its estimator. Loop bounds, buffer sizes, array
  indices and a wire format's own field widths are not decision code.
- **No placeholders, no shortcut code, no hardcoded values (RL-058, RL-062).**
- **Names state what the thing does (Rule 7).** Files named for responsibility,
  functions verb + object, predicates as questions, and a name that hides a write
  is a bug.
- **Never write state under `/tmp`** — tmpfs on this box. The tape root goes
  through `require_durable_directory`.
- **Thread caps before numpy** — the five BLAS variables set to `1` before any
  numpy import anywhere.
- **Verification is not inference (Rule 0).** Every claim has a command that ran.
- **The suite stays clean under `-W error::ResourceWarning`.** Six phase 0 tasks
  were sent back for leaking a handle; a stream reader owns sockets and files, so
  this bites harder here than it did there.
- **Every part carries a literal `PART_DECLARATION`** whose `consumes` and
  `produces` equal the blueprint's (RL-067), checked by the RL-070 wiring probe,
  which reads it with `ast` and never imports it. A declaration that is not a
  literal fails the check by design.
- **No part imports a venue module.** Parts take an adapter; the adapter set comes
  from settings (spec §3).
- **Public endpoints only.** Nothing in phase 1 holds an API key.

---

## Tasks

### Getting the tape running

**1. Dependencies, pinned with reasons.**
`ccxt` for REST (symbol lists, klines, snapshots — a solved problem worth not
rewriting) and a websocket client for streams. Each pinned exact in
`pyproject.toml` with a written reason (RL-065), each verified to install as a
`cp314` wheel on this box before it is admitted. If either needs a source build
it cannot be used and the plan changes here rather than later.
*Done when:* both import under `.venv/bin/python`, versions recorded, suite green.

**2. The tape.** `runtime/tape.py` — the index+blob pair of spec §2.2, written
through `CacheReleasingWriter`, rolled at UTC midnight, one open pair per
(venue, symbol).
*The properties that must be tested, not assumed:* a torn tail index is detected
and the partial record ignored; blob-before-index ordering holds so no index
record ever points past the blob's end; a day roll opens a new pair without
losing a message; a tmpfs root is refused. Test the torn tail by truncating a
real file, not by mocking one.

**3. The venue adapter shape.** `runtime/venues/venue_adapter.py` — the question
set of spec §3.1, and the settings entries of spec §4.3 installed into
`settings/runtime.example.toml`, the live settings directory, and
`docs/settings-schema.md`. Plus the §3.2 test that enumerates the adapters named
in settings and asserts each answers every question.

**4. Binance USDⓈ-M adapter.** Routed `/market` path from the first line — spec
§1.1's silent-failure trap. Subscription phrasing, fit against 1024 streams,
sequence and venue-timestamp extraction, closed-candle flag, ban signals
(418/429, `Retry-After`), and its trade fidelity declared as venue-aggregated.

**5. Bybit v5 linear adapter.** Same question set. Fit computed against the
**21,000-character** `args` cap — a character count, never a symbol count.
`confirm: true` for closed candles, the book's update id for §6's continuity,
ban signals (403, `20003`), fidelity declared as every-print.

**6. The connection.** Reconnect with backoff from
`venue_reconnect_backoff_floor`, treating Binance's **24-hour forced disconnect
as routine rather than as an error**, and respecting Bybit's 500-connections-per-
5-minutes so a reconnect storm cannot become self-harm. Ping/heartbeat per venue.

**7. `venue-trade-stream-reader` — the first real part.**
Its `PART_DECLARATION` matches the blueprint. Runs under `run_part`, switched by
`control_channel`, writing to the tape.
**The tape starts here.** Once this is reviewed and running, capture is live and
everything after is built alongside it.

### Built while the tape runs

**8. `symbol-catalogue-reader`** — venue symbol lists on an interval, the §4.1
selection policy, `symbol-universe`. Count `0` means the full universe.

**9. `stream-budget-planner`** — streams to connections within adapter limits and
the real `hardware_facts` measurement. Must **refuse to plan** when a capacity
fact is `None` rather than guess (spec §9).

**10. `ccxt-venue-reader`** — 1-minute candles, closed-candle flags per venue.

**11. `order-book-reader`** — shallow book at `book_depth_levels`.

**12. `feed-gap-detector`** — silence past `feed_gap_threshold`, **and** the spec
§6 sequence discontinuity. The resync must be recorded on the tape: a book
rebuilt from a gap is not the same object as one that never gapped.

**13. `feed-jump-detector`** — a candle whose open does not meet the prior close.

**14. `tick-size-resolver`** — the venue's declared increment, or one inferred
from live bid/ask spacing.

**15. `ban-signal-detector`** — 418/429/403, `Retry-After`, withheld streams into
`venue-standing`. Consumes `venue-rate-budget`, which phase 1 feeds from what the
venues actually report (spec §9).

**16. `venue-pool-rotator`** — request classes spread by standing and headroom.

**17. `api-key-pool-rotator`** — real rotation logic against an **empty key set**.
This is not a placeholder: the logic is complete and the set is honestly empty,
which is what RL-062 asks for.

**18. `cross-venue-price-consolidator`** — one price per symbol with each venue's
weight and staleness.

**19. `feed-coverage-auditor`** — per symbol, which venues supply which data and
**where none does**. This is the block's Rule 8 part: an uncovered symbol renders
as uncovered, never as absent.

### Closing

**20. Probes and the dots.** Each part's health onto the board. As parts land
their RL-070 dots turn green, and `market-data-feed` is the first block that can
go green at all — it will be the first non-red thing on the board since the
project began.

---

## Definition of done for phase 1

- The tape has been recording continuously from two venues, and the record shows
  when it was not.
- All 13 parts implemented, tested against **real captured data** (RL-063), never
  invented fixtures.
- `check_contracts.py` holds; every part's real wiring equals the blueprint's.
- The suite green with the cgroup and slow tests included, clean under
  `-W error::ResourceWarning`.
- `market-data-feed`'s 13 dots green, and the block green.
- Every deferred item recorded, not silently carried.

## What phase 1 still will not do

Trading, keys, backfill, live-tape compression, the full universe (blocked on the
file-descriptor ceiling of spec §4.2), and the governor. Spec §10 is the list.
