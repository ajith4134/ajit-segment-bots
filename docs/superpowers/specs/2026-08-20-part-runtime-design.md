# The part runtime

**Status:** approved in conversation 2026-08-20. Supersedes nothing; this is the
first implementation spec in the project. **Amended the same day**: the four questions
§15 left open are answered by measurement, §6's throttling restriction was re-examined
and kept, and three claims measured false are corrected in place — §1 rule 3 (OpenBLAS
is not lazy), §3 (a placement that returns rc=0 can still not have happened), and §5
(a part's memory limit counts the page cache it dirties). The evidence is in
`measurements/2026-08-20-part-runtime/`.

**What it decides:** what a part physically *is* at runtime, where its on/off
switch sits, how the resource governor answers scarcity, and where a part's state
lives. Everything in `docs/features.json` is built on top of this.

**Rulings it implements:** RL-058 (professional standard, no shortcuts, no
hardcoded values, no placeholders), RL-062 (no-placeholder binds futures),
RL-063 (tests on real data), RL-064 (Python core, Rust later on measurement),
RL-065 (proven libraries for solved problems), RL-066 (the transistor is hardware
governance: a part is a process, the governor owns the switch, scarcity is never
answered by a queue), RL-067 (built code matches the blueprint), RL-068 (build
order), RL-069 (the substrate is off-diagram).

**Rules it must not break:** T-1 through T-6 (`docs/transistor-rule.md`), R-01
(`docs/contracts.md`), and Rule 8 (a display shows measured state).

---

## 0. The machine, measured

Every capacity number in this document comes from this table. Nothing here is
estimated, and the first four rows correct figures used earlier in design
discussion.

| Fact | Value | How it was established |
|---|---|---|
| Machine type | `e2-custom-12-30720` | GCP metadata service |
| CPU | AMD EPYC 7B12 (Rome) | `lscpu` |
| **Physical cores** | **6**, with SMT2 → 12 logical | `lscpu`: 1 socket × 6 cores × 2 threads; `cpu0` siblings `0,6` |
| Cache | L1d 192 KiB (6), L2 3 MiB (6), **L3 32 MiB as 2 × 16 MiB** | `lscpu` |
| NUMA nodes | 1 — one memory controller for everything | `lscpu` |
| RAM | 30 070 MB, **no swap** | `free -m` |
| Disk | 239 GB free | `df -h /` |
| Python | 3.14.4 system; 3.14.6 free-threaded via `uv` | `python3 -V`; `sysconfig.get_config_var('Py_GIL_DISABLED') == 1` |
| numpy / BLAS | 2.5.2, `scipy-openblas` 0.3.34.0.0 | `numpy.__config__` |
| cgroup v2 | present; `cpu`, `memory`, `pids` delegated | `/sys/fs/cgroup/user.slice/user-1001.slice/…/cgroup.controllers` |
| `cpuset` delegated | **no** — pinning must use `sched_setaffinity` | same file; `os.sched_setaffinity` tested working |
| `systemd-run --user --scope -p …` | works, exit 0 | run directly |
| PSI | `/proc/pressure/cpu` mode `0666`, world-readable | `ls -l`, `cat` |

E2 is GCP's cost-optimised family: vCPUs are not mapped to dedicated physical
cores, so throughput carries host-level variance this project cannot observe or
control. Capacity planning treats 6 physical cores as the ceiling and expects
less under contention.

---

## 1. A part is one OS process

Forced by T-3. A thread that turns off still holds its share of a shared heap, so
its memory never returns to the kernel; only a process exiting gives memory back.
The unit of switching must therefore be a process.

Three substrates were measured before choosing. All numbers from this box.

| Substrate | switch-on | numpy | RAM returns on off | crash isolation | per-part cgroup |
|---|---|---|---|---|---|
| subinterpreter (PEP 734) | 16.5 ms | **refuses to load** | partial | partial | no |
| thread, free-threaded 3.14t | 0.7 ms | works | **no — shared heap** | **none** | no |
| **process via `forkserver`** | **2.78 ms** | works | **yes** | full | yes |

numpy's refusal is exact and disqualifying:

```
module numpy._core._multiarray_umath does not support loading in subinterpreters
```

This is numpy lacking multi-phase module init (PEP 489), which subinterpreters
require. Free-threading does not fix it — PEP 703 and PEP 684 are unrelated
mechanisms.

### Why `forkserver` and not a hand-rolled zygote

A warm parent that forks was measured at 3.0 ms and **0.76 MB PSS** per idle
child, against `forkserver`'s 2.78 ms and **3.07 MB PSS**. The hand-rolled zygote
is four times cheaper per idle part — 228 MB versus 921 MB across 300 parts — and
it is still the wrong choice.

The reason is that fork's safety condition cannot be verified once and trusted.
POSIX:

> "A process shall be created with a single thread. If a multi-threaded process
> calls `fork()`, the new process shall contain a replica of the calling thread
> and its entire address space, possibly including the states of mutexes and
> other resources. Consequently, to avoid errors, the child process may only
> execute async-signal-safe operations until such time as one of the exec
> functions is called."

Any mutex another thread held at the instant of fork is copied **locked**, and the
thread that would release it does not exist in the child. The result is a hang,
not a crash. The condition must hold at *every* fork, for the life of the process,
against every library any dependency pulls in — including threads nobody started
deliberately.

That is not hypothetical. numpy issue #30092 documents OpenBLAS 0.3.30's own
`pthread_atfork` handler deadlocking on a `server_lock` mutex, hanging the
**parent**, worsening with core count — introduced by one dependency bump.
Re-run on this box 2026-08-20, the reproducer prints `NO DEADLOCK`, because we
ship OpenBLAS 0.3.34 where it is fixed. A hazard that arrives and departs with
dependency versions is exactly the hazard a runtime should not be built on.

CPython's own maintainers reached the same conclusion: `forkserver` is the default
POSIX start method from 3.14 (gh-84559), and `os.fork()` raises
`DeprecationWarning` when threads are detected, with this text from
`Modules/posixmodule.c`:

```
"This process (pid=%d) is multi-threaded, use of %s() may lead to deadlocks in the child."
```

`forkserver` moves the single-threaded invariant into a maintained component whose
only job is to hold it. From the docs:

> "The fork server process is single threaded unless system libraries or
> preloaded imports spawn threads as a side-effect so it is generally safe for it
> to use `os.fork()`."

The 693 MB difference across 300 parts buys that guarantee. On a 30 GB box it is
affordable, and it is the cheapest insurance in this design.

### Rules the forkserver must obey

Verified constraints, not preferences:

1. **`set_forkserver_preload(['numpy', …])` must be called before the forkserver
   process launches.** Confirmed working: a child reports `numpy` already in
   `sys.modules`, inheriting the import through copy-on-write.
2. **Every part's entry point is module-level.** `forkserver` pickles the target
   by qualified name; a nested function fails with
   `AttributeError: module '__mp_main__' has no attribute '…'`. Measured here.
3. **`import numpy` alone spawns 12 OpenBLAS threads on this box unless the
   thread caps are already in the environment — so rule 4 is what makes the
   forkserver safe, not the order of operations in the preload.** An earlier draft
   of this spec said OpenBLAS spawns its worker threads lazily at first
   computational use and not at import, and therefore that the preload only had to
   avoid a warm-up matmul. **That is measured false here and the claim is
   withdrawn.** With the caps unset, `/proc/self/stat` field 20 reads **12
   immediately after `import numpy`**, before any array maths, on both the standard
   and free-threaded builds, and `threadpoolctl` reports `openblas num_threads=12`
   at that point. With `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` exported
   **before** the interpreter imports numpy, field 20 stays **1** after the import
   and stays 1 after a 600×600 matmul. Note also that `threading.active_count()`
   reports 1 in every one of those cases, which is exactly why rule 5 reads the
   kernel's count and not Python's.

   Two consequences. The caps in rule 4 are **load-bearing for fork safety**, not
   merely a scheduling preference — an unset cap does not make parts slower, it
   makes the forkserver unforkable. And the check in rule 5 stops being a rare
   safety net: it is the thing that catches a missing environment variable on the
   first fork instead of on a hang weeks later. Avoiding a warm-up matmul in the
   preload remains correct, but it was never sufficient on its own.
4. **`OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` are set before numpy is
   imported anywhere.** See §6.
5. **The fork site checks field 20 of `/proc/self/stat`** (the kernel's real
   thread count, which catches native C threads, not only `threading` objects) and
   refuses to fork above 1, loudly. This is the same check CPython's own warning
   uses. It converts a rare unreproducible hang into an immediate logged refusal.

---

## 2. States stay `off` and `on`

`state_vocabulary` in `features.json` is `["off", "on"]` and **stays that way. No
blueprint edit is required.**

An earlier draft proposed a third `standby` state to hide process-spawn latency.
At a measured 2.78 ms to start a part, that latency does not need hiding. `off`
therefore means what T-3 says it means: the process does not exist, its RAM has
returned to the kernel, and `off-state-verifier` can prove it.

Measured: 60 idle children, then killed — `MemAvailable` returned from 25 476 MB
to 25 458 MB. Memory comes back.

A part's **tick rate** is a parameter of the `on` state, not a state. It is
declared, measured, and displayed (§5), but it does not multiply the state
vocabulary, and T-5 stays intact.

---

## 3. The switch: control plane separate from data plane

**One control socket per part, owned by the governor.** Each part process is
started with an inherited file descriptor for an abstract-namespace Unix socket.
The part's loop selects on `{control fd, data inputs}`. `gate-actuator` writes one
framed command onto that fd; that is the entire switch.

A part cannot open another part's control fd because it was never given one. T-2
("only the resource governor switches parts") and T-4 ("a part knows nothing about
the circuit") are enforced by the kernel's fd table, not by code review.

The data plane is separate transport, addressed by data type per R-01. A part
never receives a command on its data path and never emits data on its control path.

### Turning a part on is two steps, both measured

A `forkserver` child inherits the forkserver's cgroup, and raw `mkdir` under the
systemd-owned session scope is refused — so a forked part does **not** get its own
resource limits for free. It is moved into one.

An existing PID can be placed in a fresh transient scope, unprivileged, through the
user bus:

```
busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
  org.freedesktop.systemd1.Manager StartTransientUnit "ssa(sv)a(sa(sv))" \
  "<part>.scope" "fail" 3 \
  "PIDs" "au" 1 <pid> \
  "MemoryMax" "t" <bytes> \
  "CPUWeight" "t" <weight> 0
```

Verified on this box: 10 of 10 PIDs moved, **5.6 ms each**, landing in
`user@1001.service/app.slice/<part>.scope` with `memory.max` and `cpu.weight`
applied as given, `pids.max` present, and a per-part `cpu.pressure` file — which is
the scarcity signal §5 depends on.

So the full switch-on cost is **2.78 ms to fork plus 5.6 ms to place ≈ 8.4 ms**,
against 158 ms for a cold spawn. `gate-actuator` records both steps; a part running
outside its own scope is a fault, because it is a part the governor cannot bound.

**`gate-actuator` must confirm the placement by reading `/proc/<pid>/cgroup`, because
the D-Bus call reports success before the move is attempted.** `StartTransientUnit`
queues an asynchronous job and returns its object path; a job that then fails to move
the PID still leaves the caller holding **rc=0**. Observed directly here once: a
placement returned rc=0 in 7.6 ms while the child stayed in `session-9.scope` with
`memory.max` unset, and the user manager's journal carried the real outcome —

```
Couldn't move process … to requested cgroup '…/app.slice/…scope'
    (directly or via the system bus): No such process
Failed to add PIDs to scope's control group: Permission denied
Failed with result 'resources'.
```

That failure did not reproduce: **87 of 88 subsequent placements succeeded**, across
forkserver children at settle delays of 0, 1, 5, 25 and 100 ms (15 each, 75/75) and a
further 12 fresh-exec children at zero settle (12/12). Fork stayed at a 2.3 ms median
and placement at a 5.7 ms median with a 6.9 ms p95, which reproduces the 2.78 + 5.6 ms
figures above. So the migration is reliable and needs no settle delay — but a rate of
roughly one silent failure in ninety, on the path that puts a part under its resource
limits, is answered by verification rather than by trusting the return code. An
unverified placement is a part the governor believes it has bounded and has not.

**Serialization cost is not a concern, and an earlier claim that it was is
withdrawn.** Measured on this box: pickle over a pipe moves a normalised bar in
**1.53 µs** (655 000 messages/second); a shared-memory ring does it in 0.78 µs.
Intraday bars at 1m/5m/15m/30m (RL-043) need on the order of one message per
second. The ring is used where a part is genuinely hot; everywhere else the simpler
transport is correct, and choosing the complex one would be optimisation without a
measurement behind it.

---

## 4. State is externalised, never checkpointed

**Design rule: a part owns no state that is not already durable outside it.**

Turning a part off is letting its process exit. Turning it on is ordinary startup:
remap its numeric state, reopen its store, reconnect its feed, resubscribe. **There
is no restore path, because startup already is one.**

This follows Candea & Fox, *Crash-Only Software* (HotOS-IX 2003):

> "Crash-only programs crash safely and recover quickly. There is only one way to
> stop such software—by crashing it—and only one way to bring it up—by initiating
> recovery."

> "To make components crash-only, we require that all important non-volatile state
> be kept in dedicated state stores, that state stores provide applications with
> the right abstractions, and that state stores be crash-only."

Their off switch is defined as external to the component — "`kill -9`… not invoking
any of the component's code". That is T-3, reached independently 23 years earlier.

### Why not checkpoint/restore

**CRIU is blocked on this machine, twice, independently:**

1. No sudo and no `apt-get`, so it cannot be installed; and `setcap` needs
   `CAP_SETFCAP`, which needs root — so the normal non-root deployment path is
   closed at the root cause.
2. `kernel.apparmor_restrict_unprivileged_userns = 1` on this box. AppArmor moves
   any user-created namespace into a profile named `unprivileged_userns` whose body
   is `audit deny capability,` with no allow line — a blanket denial of every
   capability, `CAP_CHECKPOINT_RESTORE` included. Verified:
   `unshare -U` succeeds, `unshare --user --map-root-user` fails with
   `write failed /proc/self/uid_map: Operation not permitted`.

Even with root it would be the wrong tool here. CRIU issue #2386 (open) shows that
checkpointing a forked tree **multiplies memory**: 3 GB of copy-on-write pages dump
as 6 GB, and 19 forked children ballooned to 40 GB. Cause: CRIU reads `smaps`
permission bits, where forked shared-anonymous memory carries the same `rw-p` flags
as truly private memory, so its `vmsplice()` transfer forces the very copy-on-write
it was avoiding. That is our exact topology, on a box with **no swap**. CRIU also
requires parent-child relations to stay intact, so dumping one part independently of
the forkserver is not a supported operation.

**Serialization libraries do not solve it either.** Tested on this box with stdlib
`pickle`: `socket` objects, `threading.Lock`, `threading.RLock`, generators, open
files, unstarted `Thread`s and lambdas all refuse to pickle. `dill` extends the
function-by-value cases and still cannot do frames, generators or tracebacks;
`cloudpickle`'s own README says *"Using cloudpickle for long-term object storage is
not supported and strongly discouraged"*; `joblib` is a pickle wrapper for large
arrays and does not touch the problem. The blocking issue was never "can an array be
serialized" — it is that a socket's state lives in the kernel and in the exchange's
peer, not in the Python object. No library closes that.

### Where state actually lives

| State kind | Store | Turning off | Turning on |
|---|---|---|---|
| Numeric arrays of fixed shape and dtype — rolling windows, learned parameters | `numpy.memmap` or `multiprocessing.shared_memory` | unmapping *is* the flush | remap |
| Structured records — settings, ledgers, provenance, metadata | SQLite or LMDB | committed already | reopen |
| Soft state cheap to recompute from a short replay window | nothing — not persisted | discard | replay from the tape |
| Sockets, locks, thread handles | never persisted | closed by process exit | reconnect on startup |

Two verified caveats to design around: `numpy.memmap` has **no API to explicitly
close the underlying mapping** — cleanup is refcount best-effort, so it must be
tested under a real off/on cycle before being relied on; and `dtype=object` arrays
cannot be memory-mapped at all, since they are arrays of pointers into the Python
heap.

---

## 5. Scarcity is answered instantly, never by a queue

RL-066 requires that a part can be operated "without queuing or waiting for the
cores and ram or hardware to free". This section states honestly what that can and
cannot mean.

### What cannot be escaped

**When offered demand exceeds capacity, the excess must wait, be refused, or be
served at reduced quality.** That is arithmetic, not an engineering gap, and no
mechanism surveyed avoids it — they only choose *which*, for *which work*, on
*which measured signal*.

So the achievable and correct goal is **no unbounded and no hidden queueing**: every
request is answered immediately with `ON` or `REFUSED(reason, what-it-lost-to)`, and
a refusal is a state rendered on the board with its reason. Nothing is ever parked.

Real-time scheduling theory was considered and rejected for two independent
reasons: `SCHED_DEADLINE` requires `CAP_SYS_NICE` (`sched(7)`: *"A thread must be
privileged (CAP_SYS_NICE) in order to set or modify a SCHED_DEADLINE policy"*),
and the Liu & Layland admission inequality needs known periodic `Cᵢ` and `Tᵢ` that
event-driven parts do not have. Applying it would be a category error, not merely
overkill.

### The mechanism, all unprivileged and verified here

| Purpose | Mechanism | Verified detail |
|---|---|---|
| Scarcity signal | per-part-cgroup `cpu.pressure` | world-readable; the per-cgroup `full` line measures every task in the cgroup stalled at once. The **system-wide `full` line is always zero by definition**, so only the per-cgroup file is usable |
| Memory pressure | `memory.pressure` (read-only), `MemAvailable` | — |
| Hard CPU ceiling | `cpu.max` as `$MAX $PERIOD` µs | `CPUQuota=25%` read back as `25000 100000` |
| Proportional share | `cpu.weight`, range **[1, 10000]**, default 100 | matters only under contention |
| Per-part limits | `systemd-run --user --scope -p MemoryMax= -p CPUWeight= [-p CPUQuota=]` | raw `mkdir` under the systemd-owned scope is refused even with `cpu memory pids` delegated — systemd owns that subtree, so parts are launched through it, never by hand-rolled cgroupfs |
| Core pinning | `sched_setaffinity` | `cpuset` is **not** delegated; affinity is |
| Fork-bomb breaker | `pids.max` | enforcement is `fork()` returning `-EAGAIN`; free when unused |

Admission is arithmetic against measured demand, so it is answerable immediately:
admit if the sum of measured CPU-seconds per second fits the remaining budget of
**6 physical cores**. If it does not, `switching-planner` evicts by
`part-priority`, then admits. It never queues.

### A part's memory limit counts the page cache it dirties

A part's `memory.max` does not bound its heap. It bounds its heap **plus the page
cache the part dirties by writing**, and on this box there is no swap to absorb the
difference. A part that writes more than its own memory limit is therefore killed by
the kernel unless it hands those pages back as it goes. Measured on ext4 under
`MemoryMax=200M` with `MemorySwapMax=0`, writing 500 MB:

| what the writer does | outcome | cgroup peak |
|---|---|---|
| never calls `fsync` | **OOM-killed, 3 of 3** | — |
| `fsync` every 8 MB | survives, 3 of 3 | 200 MB — pinned at the ceiling |
| `fsync` every 8 MB, then `posix_fadvise(POSIX_FADV_DONTNEED)` over the written range | survives, 3 of 3 | **16 MB** |

Dirty pages are not reclaimable, so a writer that never forces writeback outruns the
kernel and dies. `fsync` alone is enough to survive, but it leaves the part sitting at
its ceiling on clean cache that the kernel must reclaim under pressure — which means
the part's measured appetite is its limit rather than its need, and
`part-appetite-meter` would size every writer at its cap. Dropping the range after
writing it back holds the same job at **16 MB instead of 200 MB**, a twelvefold
difference in what the governor has to reserve.

So: **a part that writes a stream to disk — the tape writer above all — forces
writeback and drops its own cache at a declared interval, and that interval is a named
setting with provenance, not a literal (RL-061).** The same measurement on SQLite:
200 MB of journal appends peaks at the 200 MB ceiling under autocheckpoint, and at
**36 MB** when it checkpoints and drops cache every 8 MB, at a cost of 7.71 s against
5.01 s.

**This was nearly recorded backwards.** The first round of these tests ran under
`/tmp`, which on this box is **tmpfs** — RAM with a filesystem interface. There, every
writer is killed, including a plain file write with no database in it, because tmpfs
pages are not backed by a disk to be written to and there is no swap to evict them to.
That artefact briefly appeared to disqualify first LMDB and then SQLite. It is recorded
because the trap generalises: **no part writes state under `/tmp`,** and any
measurement of memory behaviour states the filesystem it ran on.

### Reserved floors

`resource-reservation-ledger` holds a guaranteed CPU and RAM floor for parts that
must never be starved. Parts on a floor are **excluded from adaptive allocation
entirely** — three independent research tracks converged on this. A bandit or a
successive-halving tournament cannot distinguish "consistently useless" from "quiet
because not needed yet", so a stop-loss part that has not fired looks identical to
a dead one and gets starved exactly when it becomes important.

---

## 6. What may be throttled, and what may never be

**A lower tick rate is safe only where it changes *when* an answer arrives, never
*what* the answer is.** An earlier draft treated rate reduction as a universally
safe alternative to eviction. It is not, and this section is the correction.

Each part declares two facts, and the contract checker enforces that both are
present:

- **(a)** does a lower rate change the number produced (bias or correctness risk),
  or only its arrival time (latency risk)?
- **(b)** is there a monotone invariant — sequence completeness, reconciled state —
  that a skipped tick *corrupts* rather than *delays*?

**Only parts that are latency-risk-only on both may enter a rate ladder.**
Everything else gets a reserved floor instead.

### Safe

- Draining an already-buffered or durable stream more slowly — the backing store
  tolerates a slow reader by design.
- Polling a REST endpoint no faster than its own update cadence — polling faster
  than the source changes yields no new information.
- Search-shaped computation with a proven quality curve, such as MCTS.

### Never

**Recursive (IIR) indicators.** EMA, RSI, ATR and MACD carry a seed-value bias that
decays only with sufficient history — TA-Lib documents this as the "unstable
period", and EMA(20) needs roughly 1.3 days of 1-minute bars before the bias is
negligible.

> Shrinking an indicator's window is not a coarser answer to the same question. It
> is a different, silently biased answer — and it looks identical to a healthy one
> on any board not built to catch it.

The only legitimate lever on an indicator is reducing update frequency over a
**fixed, already-warmed window** — never shrinking the window. SMA-family
indicators are exact-once-full and undefined before: a readiness gate, not a dial.

**Order-book reconstruction and balance reconciliation.** Binary correctness. CME's
MDP 3.0 recovery documentation on a sequence gap: *"it should be assumed that all
books maintained in the client system may no longer have the correct, latest
state"* — full resync, no partial credit.

**The socket read loop itself.** Coinbase documents `ErrSlowConsume`: *"market data
is not being consumed fast enough — the server-side buffer can fill, resulting in
delayed and killed connections"*. Throttling a read loop produces a hard
disconnect, not a graceful curve. Processing is offloaded so the read loop never
blocks.

### What always needs a clock

Genuinely event-driven parts burn zero CPU while idle — asyncio blocks in
`epoll_wait()` and the kernel parks the thread. But these cannot be event-driven and
must keep a timer: websocket ping/pong and forced reconnects; auth-token renewal;
**staleness and liveness detection**, where the failure mode *is* the absence of
events; periodic REST reconciliation, needed because streams drop silently; bar-close
boundaries; heartbeats on quiet symbols; retraining.

### Degradation must be visible (Rule 8)

A throttled part publishes, alongside every output, its current rate ratio and a
staleness timestamp, and the board renders both. A part running at quarter rate is
shown as such, with the declared consequence of that rate.

This is original engineering, not a borrowed pattern, and the spec says so: the
closest prior art is Brownout's dimmer (ICSE 2014), whose signal is consumed by a
load balancer rather than shown to an operator, and Google's SRE Workbook Quality
SLI — *"the proportion of responses that were served in an undegraded state"*.
Hystrix's `countSuccess` versus `countFallbackSuccess` is a usable cheap gauge.
Prometheus's `StaleNaN` does **not** solve this: it detects a metric that stopped
reporting, not a metric reporting fine about a degraded thing.

### This restriction was reviewed and kept

This section is deliberately narrower than the design first approved in conversation,
and the narrowing was re-examined on 2026-08-20 rather than inherited. It stands.

The reason is that the two errors are not symmetric. Refusing to throttle something
that could safely have been throttled costs a part being evicted instead of slowed —
visible, recoverable, and reported. Throttling something that could not costs a number
that is wrong while still looking healthy, and the failure mode of a silently biased
indicator is a trade placed on it. Since eviction plus a reserved floor already covers
every scarcity case a rate ladder would have covered, the narrow rule loses no
capability; it only moves which mechanism answers scarcity for those parts.

The rule also stays cheap to widen later. Each part already declares (a) and (b), so a
part that is genuinely latency-risk-only can be admitted to a rate ladder by changing
its declaration and nothing else — with a measurement behind the change, the way every
other number here is set.

---

## 7. Every part declares its resource class

Third declared attribute, enforced by the contract checker.

| Class | Governor policy | Why |
|---|---|---|
| I/O-bound | shared pool, generous concurrency | blocked in `epoll_wait`, costs nothing while idle |
| Compute-bound | pinned cores, BLAS threads = 1 | see below |
| **Bandwidth-bound** | **capped at 2–4 threads regardless of idle cores; two are never co-scheduled** | one memory controller, one NUMA node — they divide a fixed pipe rather than adding throughput |

Bandwidth-bound classes: elementwise ops on data exceeding cache; large copies and
dtype casts; reductions over long 1-D data; **rolling-window statistics** — the
workload this project runs most; text and CSV parsing; groupby dominated by hashing.

### BLAS threading is pinned, and the governor owns parallelism

Measured on this box, 600×600 float64 matmul in 8 Python threads:

```
BLAS threads uncapped: 1 thread  85 ms | 8 threads 356 ms | scaling 1.9x of 8
BLAS threads = 1:      1 thread 128 ms | 8 threads 233 ms | scaling 4.4x of 8
```

Eight parts each spawning a 12-thread OpenBLAS pool on 6 physical cores is 96
threads contending for 6 cores. Pinning BLAS to one thread per part more than
doubles scaling and raises aggregate throughput about 1.5x. A single part gets
slower — which is the correct trade when the governor, not the library, decides how
the cores are split.

`OPENBLAS_NUM_THREADS` is the authoritative lever for our wheel: PyPI OpenBLAS
wheels are built with **pthreads, not OpenMP**, so `OMP_NUM_THREADS` is a fallback,
not the primary control. Both are set, before numpy is imported, along with
`MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` and `NUMEXPR_NUM_THREADS`.

An earlier reading of a 2.6x scaling result as a memory-bandwidth ceiling was
wrong twice over: it was BLAS oversubscription, and the benchmark never touched
DRAM — three 600×600 float64 matrices are 8.2 MiB, which fits inside one of the two
16 MiB L3 instances. The honest compute-bound ceiling on 6 physical cores is around
4–6x, and near flat for the bandwidth-bound rolling-window work.

**`threadpoolctl` is not installed, and `numpy.show_runtime()` warns that it cannot
report BLAS thread state without it. Without it no claim about live BLAS thread
counts is verifiable** — which under Rule 0 means no such claim gets made.
`threadpoolctl` 3.6.0 is now present, and the counts quoted in §1 rule 3 are its
output rather than an inference.

---

## 8. No hardcoded values (RL-061)

No numeric literal appears in decision code. Every number is one of:

- **estimated from market data at runtime**, carrying its estimator and window; or
- **a named entry in the settings file** (RL-055), carrying its provenance.

Settings act as **hard bounds on what estimation may produce**. Estimation gives the
bot intelligence; the bound stops a broken estimator from sizing a trade at any
value. Every number can answer where it came from, and the board shows that
provenance rather than a bare figure.

This applies to the runtime itself: resource floors, rate ladders and admission
thresholds are measured by `part-appetite-meter` and `hardware-scanner`, never
written in by hand.

---

## 9. Verification (Rule 0)

No rung is inferred. Each claim in this spec has a probe that runs.

| Claim | Probe |
|---|---|
| A part is a process and `off` releases its resources | `off-state-verifier` reads `MemAvailable` and the part's cgroup before and after a switch-off, and fails if RSS did not return |
| The forkserver stays single-threaded | field 20 of `/proc/self/stat` is read at every fork site; > 1 refuses and records a fault |
| BLAS is pinned | `threadpoolctl` reports the live thread count per part process |
| The governor owns every switch | every state change traces to a `switch-record` from `gate-actuator`; a transition without one is a fault |
| Admission never queues | every admission decision records its latency and verdict; a decision without an immediate verdict is a defect |
| A throttled part is visible | rate ratio and staleness timestamp ride on every output and render on the board |
| Built code matches the blueprint (RL-067) | a probe compares each part's real `consumes`/`produces` against `features.json` and fails on divergence |
| Tests use real data (RL-063) | every test names the dated tape it replays; a test with an invented numeric fixture fails review |
| A part landed in its own scope | `gate-actuator` reads `/proc/<pid>/cgroup` after every placement — the D-Bus call returns rc=0 before the move is attempted, so the return code is not evidence (§3) |
| The thread caps are actually in effect | field 20 of `/proc/self/stat` is 1 after `import numpy`; unset caps make it 12 (§1 rule 3), so this probe catches a missing environment variable at the first fork |
| A writer is not sitting on its memory ceiling | the part's `memory.peak` against what it wrote — a stream writer that never drops its cache reads as needing its whole limit (§5) |
| Numeric state survived the last off | on switch-on, the part compares its memmap's recorded sequence stamp against the store's; a gap is a fault, not a silent reseed (§15.4) |
| A measurement was taken on real disk | every memory or durability probe records `stat -f` of the directory it used; a result from tmpfs is void (§5) |

The scripts behind every number in this spec are kept in
`measurements/2026-08-20-part-runtime/`, so each can be re-run rather than believed.

The existing `python3 dashboard/check_contracts.py` and the pre-commit hook remain
the gate for T-1..T-6 and R-01.

---

## 10. Testing on real data (RL-063)

Crypto trades 24/7, so real data is always obtainable and no test needs invented
numbers.

- **Capture.** The `market-data-feed` block records real futures data to a dated
  tape, partitioned one file per hour per venue (RL-032).
- **Replay.** Tests run against that tape. A failure therefore always means the code
  broke, never that the market moved.
- **Live conformance.** Each part additionally has a probe running against the
  current feed, reporting to the part monitor. This is what makes `RUNNING` a
  measured rung rather than an assertion.

This is not merely a preference. Because the part graph is cyclic (§11), a part can
never wait for its upstreams to exist, so its inputs must come from recorded real
data. The architecture forces the tape.

---

## 11. Build order (RL-068)

Three measurements decided this:

- **299 of 321 parts sit in one feedback cycle**; removing the entire control plane
  leaves 267 still in one. The cycle is correct — trades produce outcomes, outcomes
  produce lessons, lessons change trades.
- The transitive inputs of `paper-fill-simulator` are **306 of 321 parts**.
- Only **12 parts consume nothing**, among them `symbol-catalogue-reader`,
  `hardware-scanner` and `part-priority-reader`.

So no topological build order exists, and `features.json`'s own order is arbitrary
with respect to dependencies. Order is chosen instead by what unblocks the most
future work:

| Phase | Contents | Reason |
|---|---|---|
| **0** | This runtime, plus `threadpoolctl` and the state stores | Every part is made of it (T-1) |
| **1** | `market-data-feed` — 13 parts | Starts the tape immediately. History accrues only in real time and cannot be recovered later. Contains a zero-dependency source |
| **2** | Governor spine — `hardware-scanner`, `part-priority-reader`, `part-appetite-meter`, `switching-planner`, `gate-actuator`, `off-state-verifier` | T-2 needs the governor to own the switch; T-3 needs `off-state-verifier` for the switch to be provable. The other 8 governor parts matter only once contention exists |
| **3** | Futures vertical: opportunity-scanner → bull/bear/tailgater → arbiter → risk and capital → paper trading → ledger → observability | First real paper trade on live data (RL-024) |
| **4** | Learning loops, intelligence, LLM foundation, closed-trade decoding | These feed on trade outcomes that do not exist until phase 3 runs |

Spot and options stay `DECLARED` throughout: in the blueprint, no source file, no
fake logic (RL-062). That is measured absence, not a placeholder.

---

## 12. The substrate is off-diagram (RL-069)

This runtime is **not** declared in `features.json` and will not appear on the part
monitor. The blueprint describes the circuit; the runtime is the silicon under it.

It is still measured: it gets its own probes and its own tile on the status board,
generated like every other tile from probes that ran. This is a deliberate,
recorded exception to RL-067, not drift.

---

## 13. Dependency policy (RL-065)

Proven libraries for solved problems; own code only for what is specific to this
bot — the brains, the arbiter, the sizer, the part runtime itself.

Every dependency is pinned to an exact version with a written reason for admitting
it. The numpy/OpenBLAS deadlock in §1 is the argument: a hazard appeared in
OpenBLAS 0.3.30 and disappeared in 0.3.34, entirely through dependency versions.
Floating versions would make that invisible.

Admitted for phase 0: `numpy` (pinned 2.5.2), `threadpoolctl` (BLAS thread
verification, §7 — without it a Rule 0 claim cannot be made). Anything further is
admitted one at a time, with its reason recorded.

---

## 14. Language (RL-064)

Python is the language of the system. Whether the free-threaded 3.14t build or the
standard build is used is decided by measurement in phase 0, not now, and the
deciding factor is documented here so the decision is checkable:

- numpy's C ufunc loops already release the GIL, so multiple threads already get
  real parallelism for array maths on a standard build. Free-threading adds nothing
  there. It helps the **Python-level glue** — bar loops, dict building, object-heavy
  pandas internals.
- **Any C extension not ported to free-threading silently re-enables the GIL for the
  whole process on import.** So `sys._is_gil_enabled()` must be asserted after all
  imports, and re-asserted whenever an exchange SDK is added.
- **Free-threading's interaction with fork is undocumented.** The free-threading
  HOWTO does not mention fork at all. Since removing the GIL lets more threads be
  genuinely inside library code holding locks at any instant, it may *widen* the
  fork hazard window. This is an open risk, not a mitigated one — and it argues for
  the standard build unless the free-threaded one shows a measured win.

A part measured too slow is later rewritten in Rust behind the same part contract.
The transistor rule already permits this: a part is swappable, so its language is an
implementation detail behind its contract. Rust is admitted by measurement, never by
anticipation.

---

## 15. The four questions, answered

These were left open so they would be **decided by measurement rather than defaulted**.
They were decided on 2026-08-20, under D-011 — the user delegated the call, asking for
the best option without losing the plan's effectiveness. Every number below was measured
on this box on that date, and the scripts are kept as part of the substrate's probe suite
(§9) so each answer can be re-run rather than believed. §6 was re-examined in the same
pass and kept; the reasoning is at the end of that section.

Where a measurement turned out to be wrong it is corrected in place rather than quietly
dropped — see the tmpfs artefact recorded in §5, which briefly appeared to disqualify
both candidate state stores and was an artefact of writing to RAM.

---

### 15.1 The interpreter: the standard build, not free-threaded 3.14t

**Decided: standard CPython 3.14.** Three measurements, in descending order of weight.

**Wheels, on a machine with no compiler — this is what actually decides it.** There is
no `cc`, `gcc` or `clang` on this box and no `Python.h`, and no sudo or `apt-get` to
obtain them. Every dependency must therefore arrive as a prebuilt wheel matching the
interpreter's exact ABI tag, and a missing wheel is not "slower", it is *cannot
install*. Measured: `uv pip install lmdb` against the free-threaded venv falls through
to a source build and dies with `error: [Errno 2] No such file or directory: 'cc'`,
while the identical command against the standard venv installs `lmdb==2.3.0` from a
wheel.

Scanning PyPI's JSON API for 35 packages this project could plausibly want: of the 28
that need a compiled ABI wheel, **26 publish `cp314` and 21 publish `cp314t`**. The five
that publish `cp314` but not `cp314t` — installable on the standard build, uninstallable
here on the free-threaded one — are **`ta-lib`, `orjson`, `duckdb`, `zstandard` and
`lmdb`**. `ta-lib` is the obvious library for the indicators §6 spends its length on,
and RL-065 says solved problems get the proven library. One of the five turns out not to
matter either way: Python 3.14 ships **`compression.zstd` in the standard library**
(zstd 1.5.7, verified working here), so the tape's compression needs no third-party
package on either build.

**Performance, in the configuration this architecture actually runs.** Parts are
processes and BLAS is pinned to one thread per part, so the question is never "threads
versus the GIL" — it is "which build is faster across six processes". Pure-Python bar
aggregation over OHLCV tuples, which is precisely the glue workload free-threading is
supposed to help, with setup excluded from the timed region by a barrier, six workers on
six physical cores, five repetitions on a quiet box:

| build | one worker | 6 threads | **6 processes** |
|---|---|---|---|
| standard 3.14.4 | 9.03 M tuples/s | 8.05 M/s (0.89×) | **48.1 M/s** |
| free-threaded 3.14.6t | 7.75 M/s | 24.7 M/s (3.23×) | 42.6 M/s |

Free-threading delivers real thread parallelism and the standard build gets none; that
is not in dispute. It is the wrong axis here. **The fastest configuration measured is
the standard build across processes**, and free-threading's thread mode reaches about
half of it. Free-threading also costs roughly 14% single-threaded, in the same direction
as CPython's own documented 1–8% range.

An earlier run of this benchmark showed a far wider gap and then contradicted itself on
repetition. The cause was this session's own research agents loading the box. The table
above is from a quiet box and is tight run to run — standard 43–51, free-threaded
36–44 M tuples/s across processes.

**Risk, which points the same way.** The free-threading HOWTO does not mention `fork` or
`multiprocessing` anywhere. Two closed CPython issues make the hazard class concrete
rather than theoretical: gh-117303, a crash from `os.fork()` racing
`PyThreadState_DeleteCurrent()`, and gh-118332, a deadlock between the free-threaded
GC's stop-the-world pause and a thread attaching, in a test combining threads with
`multiprocessing`. Both are fixed in 3.13+, and `forkserver.py` is byte-identical between
the two builds, and neither build starts a background thread of its own at startup — so
this is a widened risk surface, not a live defect. It is still a widened surface on the
exact mechanism this whole runtime rests on. Separately, the free-threaded build doubles
the non-GC object header, makes every interned string immortal, and defers frees through
QSBR, all of which raise the per-part idle cost that §1 already paid 693 MB for once.

**What does not decide it:** numpy 2.5.2 is clean on the free-threaded build — it
publishes `cp314t` wheels and `sys._is_gil_enabled()` stays `False` after importing it,
measured here. The silent-GIL-re-enable hazard of §14 is real but does not fire on our
current dependency set.

**When this gets revisited:** if some part is ever *measured* to be limited by
Python-level work inside one process that genuinely cannot be split across processes.
Nothing in the blueprint looks like that today, and §14's rule stands unchanged — Rust
by measurement, never by anticipation. The project `.venv` is currently built on
3.14.6t and must be rebuilt on 3.14.4; that is phase 0 work, and it is small.

---

### 15.2 Structured part state: SQLite

**Decided: SQLite, from the standard library, in WAL mode.** LMDB was not disqualified —
it was outperformed on the things this workload is made of.

The first round of testing *appeared* to disqualify LMDB with an OOM kill under a cgroup
memory limit, then disqualified SQLite the same way, then killed a plain file write with
no database in it at all. All three were the tmpfs artefact recorded in §5. Re-run on
ext4, writing 200 MB of journal records under `MemoryMax=200M` with no swap, **neither
store is disqualified**: LMDB completes in 3.69 s, SQLite in 5.01 s at
`synchronous=NORMAL` and 5.08 s at `FULL`, all at the 200 MB ceiling, and SQLite drops to
a **36 MB** peak when it checkpoints and releases cache every 8 MB, at 7.71 s. LMDB is
about 1.36× faster at bulk append and that is a real result, honestly reported.

It loses anyway, on four grounds that matter more than append speed at this workload's
cadence — journal entries, switch records, provenance stamps and settings reads, not
per-tick data:

1. **It is not a dependency at all.** `sqlite3` is compiled into CPython; SQLite 3.46.1
   is already present. RL-065 admits a library for a solved problem, and admitting none
   is strictly better than admitting one.
2. **The read patterns are relational and LMDB has no answer to them.** "What did this
   part produce between t0 and t1" and "what is this setting now and when did it last
   change" are `WHERE`, `ORDER BY` and an index. LMDB is a sorted key/value store: every
   one of those queries becomes a hand-built composite key plus cursor discipline that
   every part must implement identically and keep correct. Encoding a time index is not
   this project's edge, so under RL-065 it is the wrong place for own code.
3. **`map_size` is a hardcoded ceiling chosen up front**, and growing it later is not
   transparent — other processes learn about it through `MDB_MAP_RESIZED` on their next
   transaction, so a resize is a fleet-wide coordination event. That cuts directly against
   RL-061.
4. **Crash-only durability is already exact.** SQLite's own documentation:
   *"Transactions are durable across application crashes regardless of the synchronous
   setting or journal mode."* A committed row survives a part being `SIGKILL`ed no matter
   how `synchronous` is set — which is precisely the off switch §4 defines. `synchronous`
   only governs power loss.

**How it is configured**, every value a named setting with provenance rather than a
literal (RL-061): WAL mode; `busy_timeout` set on every connection, because measured with
it unset a second writer fails instantly with `database is locked`, and with it set the
same writer simply waits 2.54 s and succeeds — parts should never carry bespoke retry
code for this; `synchronous=FULL` for the trade ledger, where losing a committed row to
power loss is not acceptable; `synchronous=NORMAL` for provenance, metadata and soft
stores, which is never corrupt and only risks the last write on a genuine power failure.
Write transactions stay short — open, insert, commit — because WAL permits exactly one
writer per database file and a held transaction stalls every other writer for its whole
duration.

Numeric arrays never go here; they are §15.4's business, so SQLite is never on a hot
numeric path.

---

### 15.3 Settings live at `~/.config/ajit-segment-bots/settings/`, in TOML

**Decided,** and it closes RL-055, which has been open since the design phase.

RL-055 is the constraint: *"Capital settings are edited in a settings file on the server
(SSH, or a local form through an SSH tunnel); the board shows the current values and when
they last changed."* A human edits the file in `vi` over SSH; every part reads it at
startup; the board shows the value and its change time, and under Rule 8 that display
must come from a probe, never an assertion.

**Location.** `~/.config/ajit-segment-bots/settings/`, following the XDG base-directory
convention and the precedent this project already set for the more sensitive case —
`docs/secrets.md` puts the encrypted store at `~/.config/trading/`, on the principle that
*the repo carries the inventory, the machine carries the values*. A sibling namespace
keeps the two from being confused. `/etc` is out because it needs root and there is none.
Inside the repository is out for three reasons: an operator editing over SSH would have to
remember to commit or the file silently drifts from what is deployed; a repository that is
private by policy is one settings click from not being, and these are real account numbers;
and a git-tracked file invites `git log` to answer "when did it last change", which
duplicates a job the blueprint already gave to `capital-settings-change-recorder`. The path
also contains none of the substrings the `protect-files.sh` hook and `.gitignore` block —
`secret`, `credential`, `.key`, `.pem`, `.env` — so the tooling will neither refuse to
write it nor accidentally ignore it.

**Rule 9 is satisfied by splitting the file from its schema**, exactly as `docs/secrets.md`
does: the repository carries the schema, the documented defaults and a commented template;
the machine carries the live values. Values outside git is the deliberate decision here,
not an oversight, and it is recorded so it reads as one.

**Layout — one file per scope, which is what RL-055 says and what T-4 requires.**

```
~/.config/ajit-segment-bots/settings/
    main-account.toml           # main-account-settings-reader
    segments/
        futures.toml            # the futures segment's own scope
        # spot.toml and options.toml are not created: those segments stay
        # DECLARED with no file and no code (RL-050, RL-062)
```

A single file across 27 blocks would mean one typo takes every reading part down at once,
and every reader would have to parse a shared document and pick its own slice out of it —
a part reasoning about a structure it does not own, which is a T-4 violation. Each reader
is handed its scope by the governor at launch, consistent with T-2: the control plane
decides a part's identity, never the part by discovering its siblings.

**Format: TOML, read with stdlib `tomllib`.** It has comments, so a unit and a reason sit
beside the number where the operator is already looking; it has real types, so nothing is
string-coerced downstream; and it has one unambiguous specification, unlike INI. YAML's
bare `no`/`on`/`off` booleans are a hazard in a file full of switches. JSON cannot carry a
comment, which disqualifies it for a file whose whole purpose is to be human-edited.

`tomllib` is read-only, and that turns out to be exactly right rather than a gap: **no part
ever writes this file.** `capital-settings-change-recorder` declares
`produces: ["journal-entry", "part-health"]`, so under R-01 it is structurally incapable of
writing settings back, and `check_contracts.py` would refuse any wiring that tried. No TOML
writer is needed anywhere in the runtime.

**Per entry: `value`, `unit`, and an operator-written `note`** carrying who changed it and
why — RL-061's provenance, recorded at the point the number enters the system. There is
deliberately **no self-reported "last changed" field**: a timestamp the operator maintains
by hand is an assertion, and Rule 8 does not accept assertions.

**"When it last changed" is measured, not asserted.** `watchdog` on its inotify backend
watches the *directory* — not the individual files, so an editor that saves by
write-temp-then-rename does not orphan the watch. A wake triggers a re-parse, and the
parsed structure is diffed against the last accepted value, which filters the no-op save
that mtime alone reports as a change. `IN_Q_OVERFLOW` is treated as "re-establish ground
truth" and forces a full re-read, because a dropped event must never read as *nothing
changed*. `capital-settings-change-recorder` appends the diff as an immutable
`journal-entry`, and the board queries that journal — never the file's mtime, never `git
log`. `watchdog` 6.0.0 ships a pure-Python Linux wheel, so it needs no compiler here.

**A bad edit fails closed and keeps the last known good.** A syntax error must not take the
part down — under T-3, "off" is the governor's decision and never a part's reaction to its
input. So the reader parses the candidate into a fresh value and swaps only on success;
on failure it keeps serving the last value that parsed and raises `part-health` so the
board shows the rejection and its reason. A semantically dangerous edit that parses fine is
`capital-settings-validator`'s existing job, and the existing answer stands: inconsistent
settings zero the risk limit rather than trade on a guess.

---

### 15.4 `numpy.memmap` is safe, and the missing `close()` is not a durability hazard

**Decided: file-backed `numpy.memmap` for durable numeric part state.** The worry that
opened this question is answered and does not survive contact with the measurement.

numpy's own documentation is blunt — *"Currently there is no API to close the underlying
mmap"* — and numpy#13510 has been open since 2019 because there is no safe way to add one:
a `memmap` can be aliased by other `ndarray` views, and closing under a live view
segfaults the interpreter.

None of that touches durability, because **durability here is the kernel's job, not
numpy's.** Dirty `MAP_SHARED` file-backed pages live in the page cache, which belongs to
the inode and not to the process, and `mmap(2)` states that a mapping is torn down when
the process terminates by any means. So the writeback happens whether or not a single line
of userspace cleanup ever runs — which is exactly the case under `SIGKILL`, where none of
it does.

Measured here, on ext4: a child process opens a 16 MB `float64` memmap, writes a
deterministic pattern, **does not flush**, and is `SIGKILL`ed. The parent reopens the file
and compares. **6 of 6 trials, 2 000 000 of 2 000 000 elements survived — 100%, with and
without an explicit `flush()`.** (The same test was first run under `/tmp` and had to be
re-run, because tmpfs would have proved nothing about writeback to disk — see §5.)

`flush()` therefore is not what makes state durable; it is what makes durability happen at
a *chosen moment*. It is used where write ordering across files matters, and not as a
ritual after every update.

What the missing `close()` genuinely costs is file descriptors and VMAs accumulating in a
process that opens and abandons many mappings **without exiting**. That is not this
architecture: a part is a process, it maps its state once at switch-on, and switch-off is
the process ending, at which point the kernel reclaims the fd table and the mappings
unconditionally. The rule this implies is worth stating because it is the one way to
reintroduce the bug: **the governor must not map part state into its own long-lived
process.**

`multiprocessing.shared_memory` is **not** the fallback for durable state, and the earlier
suggestion that it was is withdrawn. It is `/dev/shm`, which is RAM: it is not durable
across an off/on cycle, and memory that stays resident while a part is off is precisely
what T-3 forbids. Its resource-tracker also still carries cpython#82300, open since 2019,
where the tracker deletes a segment other processes are still using. Its correct and only
role here is hot live-to-live transport between two running parts, with `track=False`,
never as a store.

## Sources

Research corpus with full citations, source grading and UNVERIFIED sections:
`~/research/segment-bots-runtime/` (`ajith4134/trading-system-research`).

- `01-overload-admission-control.md` — admission control, PSI, cgroup v2, shed vs degrade
- `02-throttling-safety-and-degradation.md` — IIR indicator bias, order-book resync, degradation signalling
- `03-checkpoint-restore-and-state.md` — CRIU, pickle limits, crash-only software
- `04-fork-zygote-safety.md` — POSIX fork rules, CPython 3.14 forkserver, numpy#30092
- `05-numeric-scaling-ceiling.md` — true CPU topology, BLAS oversubscription, roofline

Primary sources cited inline above: POSIX fork rationale; `sched(7)`; kernel cgroup-v2
and PSI documentation; CPython `whatsnew/3.12` and `3.14`, `Modules/posixmodule.c`,
gh-84559; numpy#30092 and OpenMathLib/OpenBLAS#5170; criu#2386 and criu.org;
Candea & Fox, *Crash-Only Software* (HotOS-IX 2003); Google SRE Book *Addressing
Cascading Failures* and SRE Workbook *Implementing SLOs*; Klein et al., *Brownout*
(ICSE 2014); Coinbase websocket best practices; CME MDP 3.0 recovery documentation.
