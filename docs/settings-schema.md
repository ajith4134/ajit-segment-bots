# Settings schema — what each entry means, and why its default is what it is

**The values live at `~/.config/ajit-segment-bots/settings/`, never in this
repository.** This file is the schema and the reference; `settings/*.example.toml`
are the commented templates the operator copies from. The machine carries the
numbers that actually govern a running system; the repo carries what each number
means and how to reason about changing it.

This is the same split `docs/secrets.md` makes for credentials, for the same
reason. The project is pushed to GitHub (Rule 9), and a push cannot be recalled —
deleting a repository does not recall a clone, a fork, or a crawler's cache.
Private is not a durable safety property either: a private repository is one
settings click from public, at which point the whole history is exposed, not
just the current files. Settings values are a smaller blast radius than exchange
keys — nothing here is a credential — but `main-account.toml` carries real
capital bounds (RL-055), and a git-tracked copy would let it silently drift from
what is actually deployed, since an operator editing over SSH would have to
remember to also commit. So: **the repo carries the schema, the machine carries
the values**, exactly as section 15.3 of
`docs/superpowers/specs/2026-08-20-part-runtime-design.md` decided, and exactly
as `docs/secrets.md` already does for the encrypted credential store.

## How to read this document

Every setting is a TOML table of three keys — `value`, `unit`, `note` — loaded by
`runtime/settings_reader.py`. `load_settings_document` refuses a table missing
any of the three: RL-061 says a number is either estimated at runtime or a named
setting carrying its provenance, and a `value` with no `note` has no provenance to
show. The `note` in each template already carries the measurement or reasoning
behind that default; the columns below are the same content organised as a
reference, not a restatement in different words.

Two files, two scopes:

| File (repo template) | Installs to | Scope | Read by |
|---|---|---|---|
| `settings/runtime.example.toml` | `~/.config/ajit-segment-bots/settings/runtime.toml` | `"runtime"` | the runtime substrate itself — the governor spine and the parts that manage process placement, state storage and liveness reporting |
| `settings/main-account.example.toml` | `~/.config/ajit-segment-bots/settings/main-account.toml` | `"main-account"` | `main-account-settings-reader`, and downstream through it: `capital-settings-validator`, `capital-settings-change-recorder`, and every sizer whose output the bounds constrain |

The "read by" column names the part the blueprint (`docs/features.json`) declares
for that job. As of this task the substrate is still `DECLARED` on the part
monitor — nothing reads these files yet except the reader itself and the test
suite exercising it — so this records the intended reader per T-4 ("a part
knows nothing about the circuit"; a part reads its own scope, not one it
discovers), not a live wiring.

---

## `runtime.toml` — the substrate's own numbers

### `writeback_interval`

| | |
|---|---|
| Unit | bytes |
| Default | `8388608` (8 MiB) |
| Read by | any part that writes a stream to durable storage while running — the market-data tape writer above all |
| The bound | how much a writer is allowed to accumulate as dirty page-cache before it forces writeback (`fsync`) and drops the written range from cache (`posix_fadvise(POSIX_FADV_DONTNEED)`) |

A part's cgroup `memory.max` bounds its heap **plus** the page cache it dirties
by writing, and this box has no swap to absorb the difference. Measured on ext4
under `MemoryMax=200M`, writing 500 MB: never calling `fsync` is OOM-killed (3 of
3); `fsync` every 8 MiB survives but sits pinned at the full 200 MB ceiling,
because the pages are clean but still resident; `fsync` every 8 MiB followed by
`FADV_DONTNEED` over the written range survives the same 500 MB write at a
**16 MB** peak — a twelvefold reduction in what the governor has to reserve for
that part. `8388608` (8 MiB) is the interval that produced the 16 MB peak in that
measurement; doubling it roughly doubles the peak, since the peak is the interval
plus whatever writeback has not yet completed.

### `placement_confirmation_deadline`

| | |
|---|---|
| Unit | seconds |
| Default | `0.5` |
| Read by | `gate-actuator` |
| The bound | how long `gate-actuator` waits for a PID it just placed to actually appear inside its target cgroup scope before calling the placement a fault |

Switching a part on is two measured steps: `forkserver` produces the process
(2.78 ms), then `gate-actuator` moves it into a fresh transient scope over the
systemd user bus (5.6 ms). The D-Bus call returns success (`rc=0`) before the
move is confirmed to have happened — `StartTransientUnit` queues an async job and
a failed move can still return `rc=0`, observed directly once on this box, where
a placement reported success in 7.6 ms while the child stayed in the wrong scope
with `memory.max` unset. So `gate-actuator` must independently confirm by reading
`/proc/<pid>/cgroup`. Over 87 of 88 successful placements, confirmation measured
a 5.7 ms median and 6.9 ms p95; `0.5` s is roughly 70x the measured p95, wide
enough that a slow placement under real contention is not mistaken for a failed
one, tight enough that a genuinely stuck placement is caught inside the same
switch-on cycle rather than hanging the governor indefinitely.

### `placement_confirmation_poll_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `0.002` |
| Read by | `gate-actuator` |
| The bound | how often `gate-actuator` re-reads `/proc/<pid>/cgroup` while waiting for confirmation, before it gives up at `placement_confirmation_deadline` |

Set below the measured 5.7 ms median placement time, so a normal placement is
confirmed on its second or third poll rather than waiting out most of an interval
on every switch-on. Too short wastes CPU spinning on `/proc` reads for no benefit
at this box's placement speed; too long adds needless latency to the already-fast
common case.

### `store_busy_timeout`

| | |
|---|---|
| Unit | seconds |
| Default | `5.0` |
| Read by | every part that opens the shared SQLite store (journal entries, switch records, provenance stamps, settings-change records, capital-settings reads) — set on every connection, per section 15.2 of the runtime design spec |
| The bound | how long a writer waits on SQLite's own `busy_timeout` before a `SQLITE_BUSY` ("database is locked") failure is raised, rather than failing instantly |

SQLite WAL mode permits exactly one writer per database file. Measured with
`busy_timeout` unset, a second concurrent writer fails instantly with `database
is locked`; with it set, the same writer simply waits and succeeds — measured at
2.54 s to succeed under contention on this box. `5.0` s gives roughly double that
observed wait as headroom. The reasoning this closes off matters as much as the
number: no part is to carry its own bespoke retry loop around `SQLITE_BUSY` — the
timeout is the retry policy, set once, here.

### `fork_thread_ceiling`

| | |
|---|---|
| Unit | kernel threads |
| Default | `1` |
| Read by | the forkserver launch site — the fork-safety check the runtime spec's forkserver rule 5 requires before every fork |
| The bound | the maximum thread count, read from field 20 of `/proc/self/stat` (the kernel's real thread count, not `threading.active_count()`), that the forkserver process may carry when it forks. Above this, the fork is refused rather than attempted |

POSIX guarantees a forked child a single thread; if the parent is multi-threaded
at the instant of `fork()`, any mutex another thread held is copied **locked** in
the child, with no thread left to release it — a hang, not a crash. Measured on
this box: `import numpy` alone puts 12 OpenBLAS threads into the process
immediately (field 20 of `/proc/self/stat` reads 12 right after the import, before
any array math) unless `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` are set
in the environment *before* numpy is imported — with those caps set, field 20
stays at 1 through the import and through a 600×600 matmul. Critically,
`threading.active_count()` reports `1` in every one of those cases, which is why
this check reads the kernel's own count rather than Python's view of it. The
ceiling is `1` because that is the single-threaded invariant `fork(2)` actually
requires — not a tunable to raise, but the number that makes a violation loud
and immediate instead of an unreproducible hang weeks later.

### `part_health_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `1.0` |
| Read by | every running part, while it is on — the heartbeat cadence for its `part-health` emission |
| The bound | how often a part is expected to emit a `part-health` message while switched on; the board and the governor treat a part that misses this cadence as unmeasured or faulted, not as healthy by default (Rule 8) |

Reasoned rather than measured from a single artefact: the fastest intraday
cadence in this system is 1-minute bars, and the segment also tracks 5m/15m/30m
bars, which together need on the order of one liveness message a second to catch
a stall before it is stale relative to the fastest thing worth noticing. `1.0` s
matches that, rather than the much slower cadence a purely capital-facing part
could get away with.

### `settings_recheck_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `5.0` |
| Read by | `SettingsDirectoryWatch` (`runtime/settings_watcher.py`), and downstream through it `capital-settings-change-recorder` |
| The bound | how often the watch re-reads the whole settings directory on its own clock, independent of any inotify event — the worst-case staleness of a dropped or coalesced settings edit before this backstop catches it |

`inotify(7)` documents `IN_Q_OVERFLOW` for a dropped event, but measured directly
against this project's pinned `watchdog==6.0.0`: its own C shim discards that
marker (`wd == -1`) before it ever becomes an observable event, so nothing built
on the library can react to the kernel's own signal — confirmed by forcing a real
overflow with a raw 49152-event burst against this box's
`max_queued_events=16384`, which produced one, and then confirming the same burst
run through the real, wired watch never triggered it. The periodic re-read is the
actual guarantee instead: a document already in sync produces no diff, so any
diff this pass finds is proof, not a guess, that no event reported it first —
that is when `on_overflow` fires, alongside `on_change` reporting the real diff.
Measured: parsing a 6-entry settings file costs about 225 microseconds, so
rereading the whole directory at this cadence is negligible CPU. `5.0` s is the
cost of getting this number wrong made explicit: it is how long an operator's
edit to `main_balance` or `leverage_ceiling` — real capital bounds, RL-055 — could
sit unreported if its inotify event never arrived.

---

## `runtime.toml` — the market data feed's nine (phase 1, spec §4.3)

These are the numbers the operator actually chooses about capture. **Venue
capacity figures are deliberately not here.** The operator does not decide that
Binance allows 1024 streams per connection or that Bybit's subscribe payload caps
at 21,000 characters; those are facts the venue fixes, and they are declared as
`VenueFact`s inside each adapter, carrying the document they were read from. Both
shapes carry provenance — RL-061 is about a number being answerable for, not
about which side of that boundary it came from.

The design these serve is
`docs/superpowers/specs/2026-08-21-market-data-feed-design.md`; §1 of that
document holds the measured venue facts every one of them is sized against.

### `captured_venues`

| | |
|---|---|
| Unit | venue ids |
| Default | `["binance-usdm", "bybit-linear"]` |
| Read by | `adapter_registry.load_captured_venue_adapters`, and through it every part in `market-data-feed` |
| The bound | which venue adapters are live right now |

The user's condition on phase 1 was *"mark it so if the 2 are not enough we can
add more later"*, and this line is where that condition is discharged. A venue id
resolves to `runtime/venues/<id with underscores>.py` by convention, so adding
OKX is that module plus this line — no registry table, no part edited, and the
conformance test of §3.2 covers the new venue automatically.

It is the one list-valued setting in the schema. A bare string here would be read
as a sequence of single-character venue ids, so `read_captured_venue_ids` refuses
one by name rather than capturing eleven venues called `b`, `i`, `n`…

An id named here with no module is refused loudly at load rather than skipped,
so this line and the modules in `runtime/venues/` cannot silently disagree about
which venues are being captured. Both phase 1 venues were turned on as their
adapters landed, on 2026-08-22.

### `captured_symbol_count`

| | |
|---|---|
| Unit | symbols per venue |
| Default | `30` |
| Read by | `symbol-catalogue-reader`, applying the §4.1 selection policy |
| The bound | how many symbols per venue the tape carries |

`30` is the user's decision of 2026-08-21. `0` means every symbol the venue
lists, which is the full-universe path with **no code change at all** — the
second condition the user attached. That path is not free: 1,295 perpetuals
across the two venues means 2,590 open tape files against a 1024 soft
file-descriptor limit, so §4.2 blocks the raise on that ceiling being dealt with
first. It is stated here rather than discovered at 3 a.m.

### `symbol_selection_metric`

| | |
|---|---|
| Unit | metric name |
| Default | `"quote-volume-24h"` |
| Read by | `symbol-catalogue-reader` |
| The bound | how "top N" is ordered when `captured_symbol_count` is not `0` |

24-hour quote volume is the venue's own field and is comparable across both
venues because both quote in USDT. Getting it wrong costs capture of the wrong
symbols for as long as it stands, and that capture cannot be recovered later —
which is the asymmetry the whole phase is ordered around.

### `symbol_catalogue_refresh_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `900.0` (15 minutes) |
| Read by | `symbol-catalogue-reader` |
| The bound | how often each venue's symbol list is re-read |

Not calibrated against a measured listing rate — no such measurement exists. What
*was* measured is that the count moved while it was being measured: Binance
USDⓈ-M reported 169 `TRADIFI_PERPETUAL` contracts on one call and 170 minutes
later on the next, 2026-08-21. That single observation is the whole argument for
reading the catalogue on an interval instead of writing a symbol list into code.
Too high costs a new listing uncaptured for that long; too low costs REST weight
against a 2400-per-minute budget for a call that weighs 1.

### `book_depth_levels`

| | |
|---|---|
| Unit | levels per side |
| Default | `20` |
| Read by | `order-book-reader`, through each adapter's own supported-level mapping |
| The bound | how deep the shallow book snapshot goes |

The venues do not offer the same levels — Binance USDⓈ-M's partial-depth streams
are 5/10/20, Bybit linear's are 1/50/200/1000 — so the adapter picks its own
nearest supported level at or above this and records which it actually got.
`20` is Binance's deepest partial stream, which makes it the deepest value both
venues can serve without the two adapters disagreeing about what "shallow" means.
On Bybit depth and cadence are coupled: choosing depth chooses push rate.

### `book_snapshot_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `5.0` |
| Read by | `order-book-reader` |
| The bound | how often a book snapshot is written to the tape |

Distinct from how often the venue pushes an update. The tape carries snapshots
rather than raw deltas because a delta is only meaningful alongside the snapshot
it was applied to, and §6 has to be able to record that a resync happened — a
book rebuilt after a gap is not the same object as one that never gapped. Too
high loses book resolution permanently; too low costs tape volume against 238 GB
free and 2–4 GB/day.

### `venue_reconnect_backoff_floor`

| | |
|---|---|
| Unit | seconds |
| Default | `1.0` |
| Read by | the stream connection shared by `venue-trade-stream-reader` and `order-book-reader` |
| The bound | the shortest wait before a reconnect, doubled from here on repeat failure |

Bybit allows **500 new connections per IP per rolling 5 minutes** — 1.67 per
second across every connection this box holds — so a floor below this turns one
venue outage into a self-inflicted ban, and both venues ban per IP. Binance's
24-hour forced disconnect is routine rather than a failure and does not enter the
backoff at all; a reader that treated it as an error would back off further every
day for no reason.

### `venue_reconnect_backoff_ceiling`

| | |
|---|---|
| Unit | seconds |
| Default | `60.0` |
| Read by | `VenueStreamConnection`, and through it every streaming reader |
| The bound | the longest wait before a retry, doubling up from the floor |

**Not in spec §4.3**, which named only the floor. It is here because an unbounded
doubling is a connection that has silently stopped trying: six failures at a
1-second floor is already 32 seconds, ten is over eight minutes. The cost of
getting it wrong runs both ways — too low spends connection budget against a long
outage, too high costs capture that cannot be recovered once the venue is back.

### `stream_drain_interval`

| | |
|---|---|
| Unit | seconds |
| Default | `0.5` |
| Read by | `VenueStreamConnection.drain`, once per part tick |
| The bound | how long a stream reader blocks on its socket in one tick |

Together with `part_health_interval` this bounds how long the governor waits for
an `off` to take effect: `run_part` checks the control channel before each tick,
so the worst case is one of each. `0.5` keeps that under two seconds while still
letting a busy symbol deliver a whole batch per tick — BTCUSDT `aggTrade`
measured about four messages a second on 2026-08-22, and Bybit's 50-level book
pushes every 20 ms — so a shorter interval would cost syscalls without capturing
anything sooner.

### `feed_gap_threshold`

| | |
|---|---|
| Unit | seconds |
| Default | `60.0` |
| Read by | `feed-gap-detector` |
| The bound | how long a symbol may be silent before the silence is a `feed-gap` |

This is the detector for §1.1's live hazard: a Binance connection missing its
routed path (`/market`) **stays open, errors nothing, and delivers nothing**. So
the threshold has to be short enough to catch a socket that is healthy and empty.
`60.0` is safe at 30 symbols chosen by volume, where a genuinely silent minute is
rare. It is **not** safe at the full universe — an illiquid perpetual is quiet for
minutes at a time — so this becomes a per-symbol threshold estimated from that
symbol's own arrival rate before `captured_symbol_count` is raised. Recorded here
rather than discovered as a flood of false gaps.

### `tape_root`

| | |
|---|---|
| Unit | path |
| Default | `"~/.local/share/ajit-segment-bots/tape"` |
| Read by | every part that writes to the tape, through `tape.resolve_tape_root` |
| The bound | where the tape lives |

Expanded by the part that reads it, then put through
`storage_facts.require_durable_directory`, which refuses a memory-backed
filesystem. `/tmp` is tmpfs on this box: a tape written there would measure as
working right up until the machine restarted and it was gone — the failure Rule 8
is about, in the one place where the loss is permanent. 238 GB free on `/` against
2–4 GB/day is months of runway.

---

## `runtime.toml` — the wiring's four (phase 2, part-wiring spec §7)

These arrived with the data plane. They govern the bus a part publishes onto and
the inboxes it binds, and every default below came from a measurement taken on
this box on 2026-08-22 — the scripts are in `measurements/2026-08-22-part-wiring/`.

Two numbers the wiring spec listed are deliberately **not** here.
`launcher_placement_verify_timeout` is `placement_confirmation_deadline`, which
already exists and already means exactly that; and a separate interval for
reporting publish refusals would be a second cadence for something that rides on
`part_health`, so the health interval decides it.

### `inbox_receive_buffer_bytes`

| | |
|---|---|
| Unit | bytes |
| Default | `212992` |
| Read by | every part, once per inbox it binds — one per data type it consumes |
| The bound | how far behind a consumer may fall before its producer starts losing messages to it |

The kernel's own default. Measured, it accepts 167 messages of 222 bytes, or 278
of 160 bytes, before `sendto` returns `EAGAIN` in 1.5 µs. Raising it buys a
consumer a longer stall before loss, and costs kernel memory on every one of the
978 inboxes the blueprint implies.

**There is no value at which a permanently stalled consumer stops losing data.**
That is the point: this is a buffer, not a queue, and RL-066 forbids answering
scarcity by making the queue longer. What the system does instead is count the
loss at both ends and show it (spec §4).

### `maximum_message_bytes`

| | |
|---|---|
| Unit | bytes |
| Default | `131072` |
| Read by | every part's publisher, on every message |
| The bound | the largest frame the bus will carry; anything above it is refused and counted |

Measured: a default `AF_UNIX` `SOCK_DGRAM` pair delivers a single 131 072-byte
datagram intact and refuses anything larger; with buffers grown to 8 MiB it
carries 4 MiB. Held at the default deliberately. A payload above this belongs in a
state store with a reference published in its place (runtime spec §4), and a
ceiling sized to fit one large message is a ceiling every inbox then reserves for.

### `part_tick_floor`

| | |
|---|---|
| Unit | seconds |
| Default | `0.005` |
| Read by | `run_part`, for any part that was given input descriptors |
| The bound | the fastest a part may be woken by arriving data |

Without a floor, a producer with nothing throttling it can spin a consumer at the
rate of its own output. Measured: waiting on 20 inputs and reading one costs
4.9 µs, and the tape is recording 285.3 messages a second across 62 symbols —
about 3.5 ms apart — so a 5 ms floor batches a busy stream while adding at most
5 ms of latency to a decision.

**The floor is held on the control socket alone**, so a part waiting one out is
still switchable. A floor that made a part deaf to the governor would trade T-2
for a rate limit.

### `off_state_verify_delay`

| | |
|---|---|
| Unit | seconds |
| Default | `12.0` |
| Read by | `off-state-verifier`, and the launcher when it confirms a stop |
| The bound | how long after a part's process exits its memory is checked before the release is called complete |

T-3 says an off part releases its CPU and RAM, and this is how long the kernel
takes to make that true. Measured with all 321 parts switched off at once: 942 MB
of 1 988 MB had come back after 2 seconds, and all of it after 12. **A verifier
that reads memory once and immediately reports a leak that does not exist**, which
under Rule 8 is worse than not measuring at all — it is a red tile nobody can act
on.

---

## `main-account.toml` — the capital scope (RL-055's actual subject)

This is the file RL-055 describes directly: *"Capital settings are edited in a
settings file on the server (SSH, or a local form through an SSH tunnel); the
board shows the current values and when they last changed."* Read by
`main-account-settings-reader`; validated for consistency by
`capital-settings-validator`; every accepted change appended as an immutable
`journal-entry` by `capital-settings-change-recorder`, which is the only part
the board's "when it last changed" answer is allowed to come from — never the
file's mtime, never `git log` (which is exactly why this file is not tracked by
git in the first place).

### `main_balance`

| | |
|---|---|
| Unit | USDT |
| Default | `0.0` |
| Read by | `main-account-settings-reader`, and through it every sizer and capital-allocation part downstream |
| The bound | the real account balance the operator is choosing to make available to the system. Not measured — this is the one entry in the whole schema that is a fact about the world (the operator's actual account) rather than a fact derived from a benchmark on this box |

Ships at `0.0` on purpose: zero means nothing is allocated, which is the safe
default for a system that has not yet been told to trade with real money. The
operator sets this to the real balance only once they intend the futures
vertical to size positions against it.

### `maximum_capital_per_trade`

| | |
|---|---|
| Unit | USDT |
| Default | `0.0` |
| Read by | `capital-settings-validator`, enforced as a hard ceiling wherever a sizer's output is checked before it can become an order |
| The bound | the most any single trade may ever be sized at, regardless of what any sizer's estimation produces |

This is the settings side of RL-061's split: estimation is what gives the system
its intelligence, and this bound is what stops a broken or over-confident
estimator from sizing a trade at an unbounded value. It is a ceiling that
estimation can never exceed, not a target it aims for. Ships at `0.0`, which
with `main_balance` also at `0.0` means the system is structurally unable to
size a real trade until an operator deliberately raises both.

### `leverage_ceiling`

| | |
|---|---|
| Unit | multiple |
| Default | `1.0` |
| Read by | `capital-settings-validator`, enforced wherever a futures position's leverage is set |
| The bound | the highest leverage any futures position may carry, expressed as a multiple of unlevered exposure |

`1.0` means unlevered — the position can never be larger than the capital backing
it. This is the safe value to ship a futures-first build with: it removes
leverage as a source of loss beyond the capital committed, until an operator
raises it deliberately and with the risk understood.

---

## Why `note` is not optional

`load_settings_document` raises `SettingsParseRefused` for any entry missing
`value`, `unit`, or `note` — this is not a style preference the reader is being
lenient about. A number that showed up in a settings file with no explanation
of who set it or why is exactly the failure mode RL-061 exists to make
impossible: a literal wearing a settings file as a disguise. The `note` on every
entry above is the actual provenance carried in the shipped template; this
document exists to make that provenance easy to find and reason about, not to
duplicate it as a second source of truth that could drift from the file the
reader actually parses.
