# Part wiring — what was measured, 2026-08-22

Phase 2 wires the parts: one process per part, and a data plane addressed by data
type and computed from the blueprint. Three questions had to be answered with
numbers from this box before any of it could be designed, because each one can end
a design on its own.

Every figure below came from a script in this directory, run on this server.

    PYTHONPATH=. .venv/bin/python measurements/2026-08-22-part-wiring/<script>.py

---

## 1. The blueprint's real shape

Read from `docs/features.json` with the same producer/consumer derivation the
contract checker uses (R-01).

| | |
|---|---|
| Parts | 321 |
| Data types | 275 |
| Derived edges | 4 995 |
| …of which `part-health` | 3 531 (321 producers × 11 consumers) |
| Design-flow edges | 1 464 |
| Types with more than one producer | 25 of 275 |
| Consumed types per part | median 3, max 20 |
| Produced types per part | mean 2.07, max 4 |
| Widest fan-out | `market-data`, 3 producers × 65 consumers |
| `skipped_tick_effect` | `corrupts` 223, `delays` 98 |

Two facts drive the design. **Fan-out is concentrated**: one type reaches 65
consumers and health reaches 11 from every part. And **223 parts declare that a
skipped tick corrupts their answer**, so a dropped message cannot be a silent
event on those edges.

## 2. One process per part — measured with all 321 alive

`measure_process_per_part.py`. Every part module imported, a control socket each,
the real `run_part` loop, all of them up at once.

| | narrow preload (3 modules) | wide preload (15) |
|---|---|---|
| Parts forked / alive together | 321 / 321 | 321 / 321 |
| Forks refused | 0 | 0 |
| Fork cost | 3.46 ms median, 4.87 ms p95 | 3.67 ms / 5.69 ms |
| Switch the whole system on | **1.17 s** | 1.23 s |
| PSS per part | 5.46 MB | 5.45 MB |
| PSS, all 321 | **1.86 GB** | 1.86 GB |
| Switch the whole system off | **0.11 s**, none refused | 0.12 s |
| RAM returned 12 s after off | all of it (−37.8 MB, i.e. more available than before) | |

**Preloading more modules into the forkserver does not help.** 5.46 MB against
5.45 MB — the per-part cost is the interpreter's own private pages, not the
imports, and 5.30 of the 5.46 MB is private rather than shared. An idle part costs
seven times phase 0's 0.76 MB figure, which was measured before any part existed.

**T-3 holds, but not instantly.** Two seconds after switching all 321 off, only
942 MB of the 1 988 MB had come back; twelve seconds after, all of it had. A
verifier that reads memory once and immediately would report a leak that is not
there.

## 3. The data plane: which transport, and how it is addressed

`measure_datagram_channel.py` measured an `AF_UNIX` `SOCK_DGRAM` socketpair;
`measure_inbox_addressing.py` measured the same transport addressed by a bound
abstract-namespace inbox per (part, data type).

**Transport, per message (222-byte pickled trade):**

| | |
|---|---|
| Publish and consume, round trip | 8.6 µs — 116 000 messages/second |
| Serialise alone | 2.29 µs |
| Producer fan-out to 65 live consumers | 155 µs — 6 455 messages/second, 2.38 µs per send |
| Select over 20 input descriptors and read one | 4.9 µs |
| Largest single message | 128 KiB default, 4 MiB with buffers grown |
| Depth before a full inbox refuses | 167 messages (222 B) / 278 messages (160 B) |
| Cost of a refusal | 1.3–1.5 µs |

**Against the real load.** The tape has recorded 13 223 620 trades today across 62
symbols on two venues: **285.3 messages/second**. The widest fan-out in the
blueprint sustains 6 455/second — **22.6× headroom** — and that is with the
producer paying for all 65 sends itself. A shared-memory ring measures 0.78 µs
against pickle's 1.53 µs (runtime spec §3), a factor of two on a path with 22×
spare. Building it now would be optimisation with no measurement behind it.

**Addressing: an inbox per (part, data type), not a socketpair per edge.**

| | socketpair per edge | inbox per (part, type) |
|---|---|---|
| Descriptors, system-wide | 9 990 | 1 299 |
| Descriptors held by the widest part | **342** (`no-progress-detector`) | 20 |
| Descriptors held by a producer | one per consumer | **1**, whatever the fan-out |
| Cost per send, 65-way fan-out | 2.10 µs (connected) | 2.52 µs (`sendto`) |

A 20% higher send cost buys a producer that holds one descriptor instead of 65,
and a part whose descriptor count follows what it *declares* rather than how
popular its inputs happen to be.

**The two refusals mean different things, and both are cheap:**

| What the producer sees | What it means | Cost |
|---|---|---|
| `EAGAIN` | the consumer is on, and behind — this message is lost | 1.5 µs |
| `ECONNREFUSED` | the consumer is **off**; its address stopped existing when its process exited | 2.5 µs |

That distinction is measured, not assumed: a part's inbox address was live while
its process ran, returned `ECONNREFUSED` immediately after the process exited, and
was rebindable afterwards. So the bus can tell "the governor switched that part
off" from "the data plane is broken" without either part knowing anything about
the other, and neither answer ever makes a producer wait.

**The address is a socket file, not an abstract name.** Both were measured and
behave identically -- delivered while the part runs, `ECONNREFUSED` the moment its
process is gone, rebindable afterwards. The abstract namespace has no permissions
at all: any process in the same network namespace can send to an abstract address,
and this box carries a second human user (uid 1000). Every message on this bus is
deserialised by the part that receives it, so an address anyone may write to is an
address anyone may hand a payload to. A socket file under `$XDG_RUNTIME_DIR`
(`/run/user/1001`, mode 0700, kernel-enforced) closes that, and costs one thing:
the file outlives its process, so binding is unlink-then-bind — measured,
`EADDRINUSE` without the unlink, bound and delivering with it. `/run/user/1001` is
tmpfs, which is correct here: a rendezvous point is not state.

| | abstract namespace | socket file in a 0700 directory |
|---|---|---|
| Reachable by uid 1000 | yes | no |
| Address after the process exits | gone | file remains, sends still `ECONNREFUSED` |
| Rebinding | immediate | `EADDRINUSE` until unlinked |
| Publish to a dead address | 2.5 µs | 3.8 µs |

---

## What these measurements do not answer

- Rates above trades. Order books at depth, and the full symbol universe, are both
  larger than what the tape carries today; the 22× headroom is against **today's**
  measured load, not against a load nobody has run yet.
- Nothing here was measured under a memory limit or under CPU pressure. The
  governor's own behaviour when parts contend is phase 2's work, not this file's.
- The 5.46 MB per part is an *idle* part. What a part costs while working is the
  part's own measurement, taken per part.
