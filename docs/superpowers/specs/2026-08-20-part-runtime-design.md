# The part runtime — design

**Status:** approved in conversation 2026-08-20. Supersedes nothing; this is the
first implementation spec in the project.

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
3. **The preload imports numpy but must never execute a BLAS call.** OpenBLAS
   spawns its worker threads lazily at first computational use, not at import —
   established by numpy#30092's own reproducer. A warm-up matmul in the preload
   would put threads in the forking process, which is the hazard itself.
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
report BLAS thread state without it. It is installed as part of phase 0, because
until then no claim about live BLAS thread counts is verifiable** — which under
Rule 0 means no such claim gets made.

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

## 15. Open questions

Named rather than defaulted, each to be answered before the phase it blocks.

1. **Standard build or free-threaded 3.14t?** Decided in phase 0 by measurement of
   the actual glue workload, against the two risks in §14.
2. **SQLite or LMDB for structured part state?** Both are crash-only stores. Decided
   in phase 0 against real write patterns from the feed, not from preference.
3. **Where the settings files live on disk** (RL-055) — still open from the design
   phase, needed by phase 3.
4. **`numpy.memmap`'s lack of an explicit close** must be exercised under a real
   off/on cycle before numeric state is committed to it. If refcount cleanup proves
   unreliable, `multiprocessing.shared_memory` with explicit `unlink` is the
   fallback.

---

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
