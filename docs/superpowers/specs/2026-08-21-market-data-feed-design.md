# Market data feed — phase 1 design

**Status:** binding. Read this before writing any phase 1 part.
**Date:** 2026-08-21. **Branch:** `phase-1-market-data-feed`.
**Substrate:** phase 0, merged at `0bf7722`. Every part here is a part *on* the
diagram, unlike phase 0's substrate which is off it (RL-069).

---

## 0. What this is, and why it is first

Phase 1 builds the 13 parts of the `market-data-feed` block and the tape they
write. It is first because of a fact about time rather than a fact about
dependencies: **history accrues only in real time and cannot be recovered
later.** Every other phase can be built against a tape that already exists;
the tape can only be built by starting it.

RL-068 fixes the order. The blueprint's own structure makes the usual argument
unavailable — 299 of the 321 parts sit in one feedback cycle and the transitive
inputs of a paper fill are 306 parts — so no part waits for its upstreams. Each
is built and tested against recorded real data (RL-063), which is exactly what
this phase produces.

**Scope:** two venues, ~30 symbols, public endpoints only. The user settled all
four on 2026-08-21, and settled two of them with a condition attached:

| Decision | Condition the user attached |
|---|---|
| Binance USDⓈ-M **and** Bybit v5 linear | "mark it so if the 2 are not enough we can add more later" |
| ~30 symbols by volume | "after the 30 make sure we can try and upgrade it to the full universe" |
| Trades + 1m candles + shallow book | — |
| Public endpoints only, no API keys | — |

Those two conditions are not preferences to remember; they are **§3 and §4 of
this document**. A third venue must be a settings edit plus an adapter, never a
rewrite, and the symbol set must scale from 30 to the full 1,295 without any
part changing shape.

---

## 1. The venue facts everything else is sized against

Measured or quoted from the venues' own documentation on 2026-08-21, saved with
sources in `~/research/segment-bots-phase1/`. **Every number a part acts on
comes from here or from a settings entry citing here** — RL-061 forbids a
capacity figure invented at the point of use.

### 1.1 Binance USDⓈ-M futures

| Fact | Value | Consequence |
|---|---|---|
| Streams per connection | **1024** | One connection covers 30 symbols × 3 stream kinds with room to spare |
| Connection lifetime | **24 hours, forced disconnect** | Reconnection is routine, not exceptional — a reader that treats it as an error is wrong |
| Incoming messages per connection | 10/sec | Bounds subscribe/unsubscribe traffic, not inbound data |
| REST budget | **2400 request-weight per minute per IP** | Confirmed live via the `X-MBX-USED-WEIGHT-1m` header |
| Perpetual symbols TRADING | **570** | Live `exchangeInfo` pull, 2026-08-21 |
| Ban | 429 on limit, **418 on repeat**, 2 minutes to 3 days, escalating, per IP | Per IP, not per key — so public-only does not exempt us |

**The routed-path change, and why it is a live hazard.** Binance has split the
websocket endpoint into `/public`, `/market` and `/private`. Their own text:

> "Connections that do not include a routed path (`/public`, `/market`, or
> `/private`) […] belonging to `/market` or `/private` will not push data on
> unrouted connections."

`@aggTrade`, `@kline_*` and `@markPrice` all live under `/market`. A connection
that forgets the route **silently delivers nothing** — it stays open, it does
not error, and the tape simply has no trades in it. This is the precise failure
`feed-gap-detector` exists to catch, and it is live right now. Phase 1 uses the
routed paths from the first line of code, and the gap detector is not optional
scaffolding to add later.

**There is no raw trade stream.** Binance futures offers `@aggTrade` only —
trades aggregated per 100 ms — and there is no `@trade` equivalent. This is a
fidelity limit, not a configuration choice, and §7 says what follows from it.

**The `TRADIFI_PERPETUAL` trap, measured 2026-08-21.** Binance USDⓈ-M now lists
**~170 contracts Binance's own field calls `TRADIFI`** — traditional finance —
in the same API response as the crypto perpetuals. They are quoted in USDT and
ccxt marks them `swap` and `active`, exactly like a crypto perpetual. What they
actually are is visible only in the symbols:

```
TRADIFI_PERPETUAL   AAPLUSDT, AMZNUSDT, AMDUSDT, ASMLUSDT, ANTHROPICUSDT …
PERPETUAL           1000PEPEUSDT, 1000BONKUSDT, 1000SHIBUSDT, 1000FLOKIUSDT …
```

`AAPLUSDT` is a share tokenised and traded as a perpetual future. Nothing about
the venue's response distinguishes it from a coin except `info.contractType`.

**Ruling, 2026-08-21, from the user: capture them, do not exclude them.** The
asymmetry decides it. Capture is irreversible — a day of `AAPLUSDT` not written
today is gone permanently, and §0's whole argument is that history accrues only
in real time. Trading is entirely reversible — a filter applied whenever a bot
actually places an order, at no cost and with nothing lost.

So the catalogue includes every contract type the venue lists, and **each symbol
carries its `contract_type` through the tape** so a later phase can separate them
without re-reading the venue. What is *tradeable* is a separate decision, taken
when there is something to trade, and it is not this phase's to make. RL-006
scopes the project to crypto; capturing a tokenised share is not trading one.

`status == SETTLING` (126 symbols) is still excluded, for the unrelated reason
that those contracts are on their way to delisting — that is not a segment
judgement, it is a symbol that will stop existing.

What the venue actually holds, measured live:

| set | count |
|---|---|
| `contractType == PERPETUAL`, `status == TRADING` — crypto perpetuals | **570** |
| the same, `quote == USDT` | 527 |
| `contractType == TRADIFI_PERPETUAL`, `status == TRADING` | **~170** |
| `status == SETTLING` — excluded, on their way to delisting | 126 |

The counts matter because they size the connection budget of §4.2, and because
the difference between them is exactly the thing no naive filter reports: a
symbol set of 696 and a symbol set of 570 look equally plausible from outside.

**The count moved while it was being measured** — 169 on one call, 170 minutes
later on the next, as a listing appeared. That is the argument for §4.1 in one
observation: the symbol set is read from the venue on an interval and is never a
list written into code, because it is stale the moment it is written.

So the symbol filter is a stated rule, not an idiom: **`status` is `TRADING`,
every `contractType` kept, and the contract type recorded against the symbol.**
The adapter owns this, because it is exactly the venue-specific knowledge §3.1
says lives there and nowhere else.

### 1.2 Bybit v5 linear

| Fact | Value | Consequence |
|---|---|---|
| New connections | **≤500 per IP per rolling 5 minutes** | Reconnect storms are self-harm; back off |
| Concurrent connections | ≤1000 per IP for market data | Not a binding constraint at this scale |
| Subscribe message | **21,000 characters** of `args`, no stated topic count cap | The cap is on the *string*, so it scales with symbol-name length — compute it, never assume a symbol count |
| Trades | `publicTrade.{symbol}`, real time, batched up to 1024 per message | Every print, unlike Binance |
| Candles | `kline.1.{symbol}`, `confirm: true` marks a closed candle | The closed flag is documented and reliable |
| Book | `orderbook.{1\|50\|200\|1000}.{symbol}` at 10/20/100/200 ms | Depth and cadence are coupled — choosing depth chooses rate |
| REST | 600 requests / 5 s per IP (blanket) | The three endpoints we need are not in the per-endpoint table |
| Ban | HTTP 403, "at least 10 minutes", auto-lifted | No documented duration for a websocket-specific ban |
| USDT linear perpetuals | **725** | Live measured; the docs only say "more than 500" |

**Bybit's book is a delta stream, and nothing will resync it for us.** The
research checked Bybit's own reference SDK, `pybit`, and found **it implements
no orderbook gap detection at all**. A shallow-book capture that trusts the
library is therefore untrustworthy by construction. §6 makes the sequence check
this project's own job.

### 1.3 What the two venues jointly imply

- **1,295 perpetual symbols exist** across the two (570 + 725). "The full
  universe" in the user's condition means ~1,295 symbol-venue pairs, not 500.
  §4's symbol selection must reach that number without a code change.
- **Both ban per IP.** One box, one IP, two venues: a mistake against one venue
  does not protect the other, and both bans outlast any single part's restart.
- **Their fidelity differs.** Bybit gives every print; Binance gives 100 ms
  aggregates. The tape must record which it got rather than flatten the two into
  a shape that implies they are the same.

---

## 2. The tape

### 2.1 What a tape is required to survive

A part is `SIGKILL`ed as the ordinary way of switching it off (phase 0 §4). So
the tape format is chosen against **crash-mid-write**, not against compression
ratio or query elegance.

The research evaluated Parquet, append-only binary, SQLite, HDF5 and Arrow IPC
against that. The finding that decides it:

> **Parquet's footer is written once, at close.** A writer killed before that
> leaves a file with real data and no trailer, and it is *completely
> unreadable* — not truncated, lost. The mitigating feature
> (`FlushWithFooter`) is an open, unimplemented Arrow issue.

HDF5 fails the same way outside SWMR, and the HDF Group's own position is
"more difficult to corrupt", not safe. Arrow IPC **Streaming** genuinely
survives — it is message-framed with no footer — but Arrow IPC **File** /
Feather V2 wraps that same stream in a footer and inherits the defect exactly.

**Decision: an append-only pair of files per (venue, symbol, UTC day).** No new
dependency; `numpy` and `memmap` are already pinned and already the project's
durable-numeric substrate.

### 2.2 Why two files, and not one normalised record

A single fixed-dtype record would be simpler, and it would be wrong. Normalising
at capture destroys what the venue actually said, and RL-063 requires tests to
run on real captured data — data that must stay re-interpretable when a field we
did not think to keep turns out to matter. A normalisation bug frozen into the
tape is unrecoverable; a normalisation bug in a reader is a fix.

So the tape records **what arrived**, and normalisation happens on read:

```
tape/{venue}/{symbol}/{YYYY-MM-DD}.index    fixed-width records, memmap-able
tape/{venue}/{symbol}/{YYYY-MM-DD}.blob     raw venue payload bytes, concatenated
```

The index is a fixed numpy dtype, one record per message:

| field | type | meaning |
|---|---|---|
| `received_at_ns` | `uint64` | our monotonic-derived wall clock at receipt, not the venue's |
| `venue_time_ns` | `uint64` | the venue's own timestamp, `0` when it sent none |
| `blob_offset` | `uint64` | where the payload starts in the `.blob` |
| `blob_length` | `uint32` | how long it is |
| `stream_kind` | `uint8` | trade / candle / book — from a declared vocabulary (T-5) |
| `sequence` | `uint64` | the venue's own sequence or update id, `0` when it sent none |

**Write order is blob first, then index.** A kill between the two loses one
message and leaves an index that is a whole number of records — the reader takes
`len(index) // record_size` and ignores a torn tail, exactly as phase 0's
`numeric_state` already does. A kill *during* the blob write leaves bytes no
index record points at, which are invisible rather than corrupting. The reverse
order would leave an index record pointing into a blob that has nothing there,
which is a reader crash instead of a lost message.

**One open file per (venue, symbol)** — 60 at 30 symbols, well inside any fd
limit — rolled at UTC midnight. Day-per-file matches what NautilusTrader does
for the same read pattern; hourly would produce ~1,400 files a day at full
universe for no read benefit.

### 2.3 The writer

Phase 0 built `CacheReleasingWriter` for exactly this and its measurement is the
reason: under a 200 MB cgroup limit, writing 500 MB with no writeback is
OOM-killed 3 of 3; `fsync` alone survives pinned at the ceiling; `fsync` plus
`POSIX_FADV_DONTNEED` holds at 16 MB. A part's `memory.max` counts the page
cache it dirties and this box has no swap.

**Every tape writer is a `CacheReleasingWriter`.** A stream-writing part that
opens a plain file would ask the governor to reserve its entire limit, and would
be killed under load. The interval comes from the `writeback_interval` setting
already installed.

### 2.4 Compression

None on the live file. A closed day's blob may be compressed as a separate
offline pass — zstd at frame boundaries, so truncation costs one frame rather
than the file — but **never on a file still being written**. This is deferred:
238 GB free against 2–4 GB/day is months of runway, and an untested compression
path in the write loop is a way to lose the tape rather than shrink it.

---

## 3. Adding a third venue must be a settings edit

The user's condition: *"mark it so if the 2 are not enough we can add more
later."* That makes venue-independence a structural requirement, not an
aspiration.

### 3.1 What is venue-specific and what is not

Exactly one module per venue, and nothing else in the block knows a venue name:

```
runtime/venues/binance_usdm.py      the adapter
runtime/venues/bybit_linear.py      the adapter
runtime/venues/venue_adapter.py     the shape every adapter is
```

An adapter answers a fixed set of questions and holds all the venue's oddities:

- what its websocket URL is, **including its routed path** (§1.1's hazard lives
  here, not in a part)
- how to phrase a subscription for a stream kind and a symbol
- how to compute whether one more subscription fits (Binance: a stream count
  against 1024; Bybit: a **character count** against 21,000 — the adapter
  answers "does this fit", the caller never counts anything itself)
- how to read a sequence number, a venue timestamp and a closed-candle flag out
  of a message
- what its ban signals look like — 418/429 and `Retry-After` for Binance, 403
  and code `20003` for Bybit
- what its trade fidelity is (§7)

**No part imports a venue module.** Parts take an adapter, and the set of
adapters comes from settings. This is T-4 applied at the venue boundary: a part
names data, never a venue.

### 3.2 The test that keeps it honest

A test enumerates the adapters named in settings, and asserts each one answers
the full question set. Adding OKX means writing `okx_swap.py`, adding its name
to a settings list, and that test covering it automatically. If adding a venue
ever requires editing a part, the design has failed and the test is where that
shows up.

---

## 4. Symbols: 30 now, 1,295 without a rewrite

The user's condition: *"after the 30 make sure we can try and upgrade it to the
full universe."*

### 4.1 Selection is a setting, not a list in code

`symbol-catalogue-reader` reads the venue's full symbol list. What we *capture*
is then chosen by a named policy in settings — a count and an ordering
(by 24 h quote volume), not 30 hardcoded strings. Setting the count to `0` means
"every symbol the venue lists", which is the full-universe path with no code
change at all.

A hardcoded symbol list would also rot: listings change, and the blueprint's own
`symbol-universe` type says "refreshed as listings change".

### 4.2 What must scale with it

Three things break if they assume 30, and each is stated as a rule rather than a
number:

1. **Subscription packing.** Bybit's cap is 21,000 *characters*, so it depends
   on symbol-name length. The adapter computes fit; nothing multiplies 30 by
   anything.
2. **Connection count.** At full universe Binance needs ≥2 connections for 1,295
   streams against a 1024 cap. `stream-budget-planner` derives this from the
   adapter's limits and the measured hardware, which is why it consumes
   `hardware-capacity`.
3. **Open files and memory.** 1,295 symbol-venue pairs is 2,590 open tape files.
   That is over the usual 1024 soft fd limit, so the writer must either hold
   fewer files open than symbols or the limit must be raised deliberately. **The
   full-universe path is blocked on this and it is stated here rather than
   discovered at 3 a.m.** At 30 symbols it does not arise.

### 4.3 New settings entries

Added to `settings/runtime.example.toml` in the existing shape — `value`,
`unit`, `note`, where the note carries provenance and says what the number costs
if wrong:

| entry | unit | what it is |
|---|---|---|
| `captured_venues` | venue ids | which adapters are live; adding one is this line plus a module |
| `captured_symbol_count` | symbols per venue | `0` means the full universe |
| `symbol_selection_metric` | metric name | how "top N" is ordered; 24 h quote volume |
| `symbol_catalogue_refresh_interval` | seconds | how often listings are re-read |
| `book_depth_levels` | levels | shallow book depth; couples to Bybit's push rate |
| `book_snapshot_interval` | seconds | how often a book snapshot is written to the tape |
| `venue_reconnect_backoff_floor` | seconds | starting backoff; Bybit's 500-per-5-minutes makes a reconnect storm self-harm |
| `feed_gap_threshold` | seconds | how long a silent symbol is before it is a gap |
| `tape_root` | path | where the tape lives; must pass `require_durable_directory` |

Every one is read through phase 0's `settings_reader`, which refuses an entry
lacking `value`/`unit`/`note` and keeps the last document that parsed when an
edit is broken.

---

## 5. The 13 parts

Each part is a process on the phase 0 substrate: forked through
`forkserver_launcher`, confirmed into its own scope by `scope_placer`, switched
by `control_channel`, looping in `run_part`. Its `consumes` and `produces` must
**equal** the blueprint's declaration — RL-067, now enforced by the wiring check
added under RL-070, which reads a literal `PART_DECLARATION` from each module
without importing it.

| part | what it does here |
|---|---|
| `symbol-catalogue-reader` | reads each venue's symbol list on an interval; applies the §4.1 selection policy; produces `symbol-universe` |
| `stream-budget-planner` | assigns streams to connections within each adapter's stated limits and the measured `hardware-capacity`; produces `stream-plan` |
| `ccxt-venue-reader` | 1-minute candles per §1's closed-candle flags; produces `market-data` |
| `venue-trade-stream-reader` | trades — every print on Bybit, 100 ms aggregates on Binance; produces `market-data` |
| `order-book-reader` | shallow book at `book_depth_levels`; produces `order-book-snapshot` |
| `feed-gap-detector` | a symbol silent past `feed_gap_threshold`, **and** the §6 sequence discontinuity; produces `feed-gap` |
| `feed-jump-detector` | a candle whose open does not meet the prior close; produces `feed-jump` |
| `tick-size-resolver` | the venue's declared price increment, or one inferred from live bid/ask spacing; produces `price-increment` |
| `ban-signal-detector` | 418/429/403, `Retry-After`, withheld streams → `venue-standing` |
| `venue-pool-rotator` | spreads request classes across venues by standing and headroom |
| `api-key-pool-rotator` | built against `key-standing` with **no real keys** (§0) — the rotation logic is real, the key set is empty |
| `cross-venue-price-consolidator` | one price per symbol with each venue's weight and staleness → `consolidated-price` |
| `feed-coverage-auditor` | per symbol, which venues supply which data **and where none does** → `feed-coverage` |

`feed-coverage-auditor` is the Rule 8 part of this block: a symbol nothing covers
must render as uncovered, not be absent from the report.

---

## 6. Gaps, jumps, and sequence continuity

Three different failures, deliberately not merged:

**A gap** is silence — no message for a symbol past `feed_gap_threshold`. It
catches a dead connection, and specifically catches §1.1's unrouted-path trap
where the socket is healthy and empty.

**A jump** is present-but-discontinuous data — a candle whose open does not meet
the prior close. The blueprint's own reason is that a stop sitting in the gap
still counts as crossed, so this is a correctness fact for later phases, not a
data-quality nicety.

**A sequence discontinuity** is the one the venues will not tell us about.
Bybit's book is a delta stream carrying an update id, and its own reference SDK
does not check continuity. So: **every message with a sequence field is checked
against the last one for that symbol and stream, and a break is a `feed-gap`
naming both sequence numbers.** On a break the book is resubscribed and a fresh
snapshot taken, and the tape records that a resync happened — a book rebuilt
from a gap is not the same object as one that never gapped, and a reader must be
able to tell.

The sequence check lives in the adapter's "read a sequence from this message"
answer plus one shared comparison, not reimplemented per venue.

---

## 7. Fidelity is recorded, never flattened

Binance gives 100 ms aggregates; Bybit gives every print. Both are "trades", and
a tape that stores them identically silently claims a precision one of them does
not have.

**Each captured stream records its fidelity** — whether trades are individual
prints or venue-side aggregates — and that travels with the data rather than
living in a person's memory. A later phase computing a microstructure feature
across both venues must be able to see which is which; if it cannot, it will
compute a number that is wrong in a way nothing detects.

This is Rule 8 pointed at the tape: absence of per-print resolution is its own
state, not something to be quietly rendered as if it were present.

---

## 8. Running on the phase 0 substrate

Nothing here reimplements what phase 0 built:

| need | phase 0 module |
|---|---|
| tape writing without being OOM-killed | `page_cache_discipline.CacheReleasingWriter` |
| the tape directory is really on disk | `storage_facts.require_durable_directory` |
| every number named, with provenance | `settings_reader` |
| a settings edit noticed, a dropped event never read as quiet | `settings_watcher` |
| gap and rejection records, current-value-and-when | `state_store` |
| rolling windows | `numeric_state` |
| the part loop and its off switch | `part_process.run_part` |
| forked without inheriting a threaded parent | `forkserver_launcher` |
| bounded in its own cgroup, confirmed | `scope_placer` |
| the machine's real capacity | `hardware_facts` |

**The BLAS caps apply here too.** Any part importing numpy must have the five
variables set first — `import numpy` alone puts 12 kernel threads in a process
and makes a forkserver unforkable.

---

## 9. The two inputs phase 1 does not produce

- **`hardware-capacity`** is produced by `hardware-scanner`, a phase 2 part. But
  phase 0 already built `hardware_facts`, which is exactly what that part wraps,
  so `stream-budget-planner` consumes the **real measurement** rather than a
  fixture. Where a fact is unmeasurable it arrives as `None` and the planner must
  refuse to plan rather than guess — a core count it invented would be a
  connection budget the machine cannot honour.
- **`venue-rate-budget`** is produced by `venue-rate-budgeter` in the
  execution-venue-adapter block, which does not exist. `ban-signal-detector`
  consumes it. Phase 1 feeds it from what the venue itself reports — remaining
  weight from `X-MBX-USED-WEIGHT-1m`, and Bybit's headers — which is a *measured*
  budget rather than a stub, and the real part later becomes the thing that
  reconciles it against intended spend. **This is the one edge phase 1 stands in
  for, and it is recorded here so the substitution is visible.**

---

## 10. What phase 1 does not do

- **It does not trade, and it holds no keys.** Public endpoints only. Nothing
  here can touch an account that holds money.
- **It does not normalise into features.** The tape records what arrived;
  indicators are later phases reading it.
- **It does not backfill.** Historical klines are available over REST and are a
  separate, later job. The live tape starts now because only it cannot be
  recovered later.
- **It does not compress the live tape** (§2.4).
- **It does not open the full universe** — the 30-symbol path is what runs; §4
  is what makes 1,295 reachable, and §4.2's file-descriptor ceiling is the known
  blocker on that path.
- **It does not build the governor.** Parts run under the substrate's switch;
  deciding which parts run under scarcity is phase 2.
