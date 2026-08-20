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
