# The wiring

**Phase 2.** One process per part, a data plane computed from the blueprint, and
the governor holding the switch. The measurements behind every number here are in
`measurements/2026-08-22-part-wiring/`, taken on this box on 2026-08-22.

This spec inherits `docs/superpowers/specs/2026-08-20-part-runtime-design.md`
entirely. That one decided what a part *is*: an OS process, forked from a
forkserver, placed in its own transient scope, switched by a control socket the
governor owns, keeping no state that is not durable outside it. This one decides
what runs **between** parts, and what starts them.

Nothing here relaxes the transistor rules. T-4 in particular is the hardest
constraint on this design: a part must be wired to another part without either of
them being able to name the other, and the wiring must still be checkable.

---

## 0. What this decides

1. The uniform entry point every part gains, so 321 parts start the same way (T-1).
2. The data plane: its addressing, its transport, its framing, and what a producer
   sees when a consumer is off, behind, or absent.
3. How the wiring is computed from `docs/features.json` — never written by hand,
   never known to a part (R-01, T-4).
4. What a dropped message means, and which parts may not lose one silently.
5. Who starts and stops parts, and why that is not a part.
6. What must be measured before any of it is believed.

**What it deliberately does not decide:** which parts run in the first end-to-end
paper run (§10 fixes the method for choosing, the choice is made against the built
system), the governor's contention policy beyond the spine, and anything about
spot or options, which stay `DECLARED` (RL-062).

---

## 1. A part gains one entry point, and it is the same for all 321

Every part module today ends in a `run_<part_name>` function taking its engine, a
control socket, one or more `read_*` callables, one or more `publish_*` callables,
a health interval and an `emit_health`. All 321 have one; measured, there are 305
distinct signatures because the *names* differ, while the shape does not.

That shape is right and stays. What is missing is the step before it: something
has to build the engine, bind the readers and publishers to real channels, and
call it. Today nothing does, which is why 321 parts exist and none of them run.

**Each part module gains `start_part(context) -> int`.** Module-level, one per
part, named identically in every part — that sameness *is* T-1. It:

1. reads that part's settings through `context.settings`, so no number is a
   literal (RL-061);
2. constructs the part's engine, including its learned components (RL-060);
3. binds each `read_*` to the inboxes of the data types the part declares it
   consumes, and each `publish_*` to the bus, by data type;
4. calls the part's existing `run_<part_name>`, and returns its exit code.

**Why in the part and not in a central registry.** A registry mapping part id to
an assembly function would be one file naming all 321 parts, and every argument
for putting it there is an argument for the circuit knowing its parts. A part
already knows two things nobody else does: how to build itself, and how its input
types pair up — `read_opinions_and_context` yields four correlated values, and only
`opinion-arbiter` knows they belong together. That correlation is part logic. It
lives in the part.

**The part still names no other part.** It names data types, which is exactly what
R-01 permits, and the bus turns a type into a set of addresses. A part cannot
enumerate its peers, because `context.bus` will not tell it.

### `run_part` gains a readiness waiter

The loop today waits on the control socket with a timeout equal to the tick
interval, and calls `do_one_tick` when it returns. So a part ticks on a clock and
never on the arrival of data: a fill would sit in an inbox until the timer came
round. `run_part` takes an optional waiter that selects over `{control fd} ∪
{input fds}`, so a part wakes when its data arrives *or* its timer expires,
whichever comes first. The control socket keeps its priority: a part that woke on
data still checks control before ticking, or T-2 is lost the moment the system
gets busy.

The parameter is optional and defaults to today's behaviour, so a part with no
inputs — measured, 12 parts consume nothing — is unchanged, and so is every
existing test.

---

## 2. The data plane

### 2.1 One inbox per (part, data type)

**A part binds one datagram socket for each data type it declares it consumes.**
That is its inbox for that type. A producer holds a single unconnected datagram
socket and sends to the inbox address of every consumer of the type it is
publishing.

The alternative — a socketpair per edge — was measured and rejected. The blueprint
derives 4 995 edges, so it would cost 9 990 descriptors, and it distributes them by
*popularity of input* rather than by what a part declares: `no-progress-detector`
would hold 342 descriptors, 321 of them to read one low-rate type. Inboxes cost
1 299 descriptors system-wide, at most 20 for any part, and exactly **one** for a
producer whatever its fan-out. The price is 2.52 µs per send against 2.10 µs
connected — 20% — on a path with 22× headroom.

### 2.2 The address is a socket file, not an abstract name

    $XDG_RUNTIME_DIR/ajit-segment-bots/inboxes/<part-id>.<data-type>

The directory is mode 0700. Both addressing schemes were measured and behave
identically on delivery and on the off signal; they differ on who may write. An
abstract-namespace address is reachable by every process in the network namespace,
this box carries a second human user (uid 1000), and **every message on this bus is
deserialised by the part that receives it**. An address anyone may write to is an
address anyone may hand a payload to. The directory's mode is enforced by the
kernel and closes that.

`/run/user/1001` is tmpfs. That is correct and not a breach of the never-write-
state-to-tmpfs rule: a rendezvous point is not state. No part's state lives here —
§4 of the runtime spec already fixed where state lives.

The cost is that a socket file outlives its process. Binding is therefore
unlink-then-bind, and **the launcher does the unlink, before the fork** — a part
must never remove an address it does not own, and the launcher is the only
component that knows a part is not currently running.

### 2.3 Transport and framing

`AF_UNIX` `SOCK_DGRAM`, non-blocking on the sending side, message boundaries from
the kernel. **One datagram per item, never per batch**: an inbox's depth is
measured in messages, and a batched datagram would turn one refusal into the loss
of everything in it.

Each datagram carries a header and a payload. The header states:

| Field | Why it exists |
|---|---|
| `data_type` | the address already implies it; carrying it makes a misdirected message detectable rather than silently mis-parsed |
| `producer_part_id` | so a consumer can attribute a gap to a producer without the producer naming the consumer |
| `sequence` | per (producer, type), monotonic, gapless — this is what makes loss *detectable*, and §4 depends on it |
| `published_at_ns` | staleness, which Rule 8 requires a display to show |
| `codec_version` | a bus whose framing changes must refuse an old frame, not misread it |

The payload is a pickled dataclass. Measured, 2.29 µs to serialise and 8.6 µs for a
full round trip — 116 000 messages/second against the 285.3/second the tape is
actually recording. Pickle is acceptable **only** because §2.2 restricts who can
reach an inbox; the two decisions are one decision and must not be separated later.
A frame whose `codec_version` is unknown is refused and counted, never unpickled.

Payloads have a ceiling: 128 KiB by default, 4 MiB with buffers grown. Anything
larger is a fact that belongs in a state store with a reference on the bus (runtime
spec §4), and the bus refuses it rather than growing a buffer to fit.

### 2.4 Publishing never waits

The sending socket is non-blocking, always. There are exactly three outcomes, all
measured, none of which blocks the producer:

| Outcome | Meaning | Cost |
|---|---|---|
| delivered | the consumer is on and keeping up | 2.5 µs |
| `EAGAIN` | the consumer is on and **behind** — this message is lost | 1.5 µs |
| `ECONNREFUSED` | the consumer is **off**; its process is gone | 3.8 µs |

This is RL-066 as arithmetic: scarcity is answered instantly, never by a queue. A
consumer that stops reading cannot slow its producer down, and cannot make a
market-data reader miss a tick on the wire.

The bus counts all three per (data type, consumer), and those counts are what the
board reads. **A drop is never silent** — §4 says what a drop is allowed to mean.

---

## 3. The wiring is computed, and then checked against what was built

`runtime/wiring_plan.py` reads `docs/features.json` and produces, for each part:

- **inboxes**: one address per declared consumed type;
- **outbound**: for each declared produced type, the addresses of that type's
  consumers.

Three rules, each of which is a rule that already exists somewhere else:

1. **A part never receives its own message.** 15 parts consume a type they also
   produce — mostly the eleven health readers, which emit `part-health` like every
   part does and read it too. Self-delivery is removed at plan time.
2. **Peer blocks are never wired to each other** (R-03). The contract checker
   already proves no such type exists; the plan derivation asserts it again rather
   than trusting that it was checked elsewhere.
3. **The plan is derived, never authored.** There is no file in which an edge can
   be written by hand, so no edge can exist that the blueprint does not imply.

**And it is checked against the built system.** Each part carries a literal
`PART_DECLARATION`; the RL-070 probe already reads it with `ast` — never importing
it — and compares `consumes`/`produces` to the blueprint. The wiring plan is built
from the blueprint, so RL-067 is what makes the plan true of the code: a part whose
declaration drifts from the blueprint is wired wrongly *and* shows red on the
board, in that order.

---

## 4. What a dropped message is allowed to mean

The blueprint states, per part, what a skipped tick does: **223 parts say
`corrupts`, 98 say `delays`**. That field was written for the governor's throttle.
It answers this question too, and it must answer it the same way, or one field
means two things.

- A consumer whose `skipped_tick_effect` is **`delays`** may lose a message. It is
  counted, it is visible on the board, and it is not a fault.
- A consumer whose `skipped_tick_effect` is **`corrupts`** may not lose one
  silently. The sequence gap is detected at the receiving end and **the part
  reports it**: its next health report carries the loss, and the part's own logic
  decides whether its answer is still usable — exactly as `feed-gap-detector`
  already treats a venue's sequence break, because it is the same problem one layer
  in.

**Loss is detected by the consumer, not assumed by the producer.** The producer's
`EAGAIN` count and the consumer's sequence gaps are two independent measurements of
the same event, and they must agree; a divergence between them is a bus defect and
is worth a probe of its own.

**What this is not.** It is not a promise of delivery, and no amount of buffer
makes it one. An inbox holds 167 to 278 messages at default size — the depth is a
setting (§7), and raising it trades memory for a longer stall a consumer may take
before it starts losing data. There is no size at which a permanently stalled
consumer stops losing messages, and pretending otherwise by growing a queue is the
thing RL-066 forbids.

---

## 5. Off means off, and the bus can tell

When the governor switches a part off, its process exits (T-3). Its inboxes close
with it — the descriptors are the process's — and every producer's next send to it
returns `ECONNREFUSED` within 3.8 µs.

That is the property that makes a lossy bus safe to build on: **the bus can
distinguish a part that is off from a part that is broken, without either part
knowing the other exists.** A producer publishing into a system where half the
parts are off does not fail, does not block, and does not need to be told which
parts are on. It publishes to the addresses its plan gives it and counts what came
back.

Measured, and the ordering matters: the address was live while the process ran,
refused immediately after it exited, and was rebindable afterwards.

**Switching the whole system is fast enough to be routine.** All 321 parts fork in
1.17 s (3.46 ms median each) and stop in 0.11 s, with none refusing to stop. Idle,
they cost 1.86 GB of the box's 29 GB — 5.46 MB each, and preloading more modules
into the forkserver does not reduce it, because the cost is the interpreter's own
private pages rather than the imports.

**T-3 is verified with a delay, never instantly.** Two seconds after switching all
321 off, 942 MB of 1 988 MB had returned; twelve seconds after, all of it had. An
`off-state-verifier` that reads memory once and immediately would report a leak
that does not exist.

---

## 6. Who starts parts, and why it is not a part

**`runtime/part_launcher.py` is substrate, not a feature.** It is off-diagram under
RL-069, gets its own status-board tile, and never appears on the part monitor.

It owns exactly what a part cannot own:

- the forkserver, and the thread-count refusal that keeps forking safe;
- the control socket pair for each part — it holds the governor's end and passes
  the part's end across the fork, which is what makes T-2 a fact of the kernel's
  descriptor table rather than a convention;
- the unlink of a stale inbox address before a part binds it;
- placing each part in its own transient scope, and **verifying the placement by
  reading `/proc/<pid>/cgroup`** — the D-Bus call returns success before the move
  is attempted, and phase 0 measured one silent failure in ninety.

**It does not decide anything.** It starts what it is told to start and stops what
it is told to stop. The decision belongs to `gate-actuator`, which is a part, and
which reaches the launcher over the control plane like everything else. A launcher
that chose would be a second governor, and T-2 permits one.

**Crash-only, both ways** (runtime spec §4). A part that dies is restarted by
ordinary startup, because there is no other kind. A launcher that dies leaves parts
running, and its own restart re-adopts them by their inbox addresses rather than by
a file it wrote about them.

---

## 7. Every number is a setting or a measurement

RL-061 binds here as everywhere. The bus introduces exactly these numbers, each a
settings entry with a written provenance, none a literal in decision code:

| Setting | What it decides |
|---|---|
| `inbox_receive_buffer_bytes` | how far behind a consumer may fall before it loses data — measured, 212 992 B holds 167 messages of 222 B |
| `maximum_message_bytes` | the ceiling above which a payload belongs in a state store, not on the bus |
| `publish_refusal_report_interval_seconds` | how often a producer's drop counts reach the board |
| `part_tick_floor_seconds` | the fastest a part may be woken by data, so an unbounded producer cannot spin a consumer |
| `launcher_placement_verify_timeout_seconds` | how long placement in a scope is waited for before it is called a failure |
| `off_state_verify_delay_seconds` | how long after a part exits its memory is checked — 2 s reports a leak that is not there, 12 s does not |

Loop bounds, header field widths and descriptor counts are not decision code and
are not settings.

---

## 8. Health rides the same bus

`part-health` is a data type like any other: 321 producers, 11 consumers, 3 531 of
the blueprint's 4 995 edges. It is published through the same sockets, with the same
refusals, and read by the same inboxes. There is no privileged health channel,
because a privileged channel would be a second data plane and T-1 says there is one
shape.

It carries what §6 of the runtime spec requires — the part's rate ratio, its
staleness, any control frame it refused — and now also its **input loss**, per §4.

The control socket stays entirely separate and carries no data (T-2). A part that
emitted data on its control path, or accepted a command on an inbox, is a
contract violation and is checked as one.

---

## 9. Verification (Rule 0)

Nothing in this spec is believed because it is written here. Each claim has a
command, and these run in the suite:

| Claim | How it is checked |
|---|---|
| The plan equals the blueprint | derive the plan, compare to `check_contracts.py`'s own edge derivation; any difference fails |
| The built parts equal the plan | the RL-070 `ast` probe, per part, on `PART_DECLARATION` |
| A part never receives its own message | assert no self-edge survives derivation, on the 15 parts that would have one |
| Peers stay unwired | R-03, re-asserted at plan time |
| Publishing never blocks | a stalled consumer, a producer measured against the clock |
| Off is distinguishable from broken | a part switched off, `ECONNREFUSED` observed, then rebound |
| A `corrupts` consumer never loses silently | drop messages deliberately, assert the sequence gap reaches health |
| Off releases memory | measured after the delay of §7, not before |
| The suite leaks no descriptors | `-W error::ResourceWarning`, as phase 0 and phase 1 both ran |

**Tests use real captured data (RL-063).** The tape holds 13 223 620 trades from
two venues, which is what flows through the bus in every test that needs a message.
No invented fixture stands in for a market.

---

## 10. Build order, and what "end to end" means

**Phase 2a — the substrate.** `runtime/bus.py`, `runtime/wiring_plan.py`,
`runtime/part_launcher.py`, and `run_part`'s readiness waiter. Nothing on the part
monitor changes, because none of this is a part. Its tile is on the status board.

**Phase 2b — the governor spine.** `hardware-scanner`, `part-priority-reader`,
`part-appetite-meter`, `switching-planner`, `gate-actuator`, `off-state-verifier`
gain `start_part` and run for real. This is the first time the switch is held by
the thing that is supposed to hold it, and the first time T-3 is proved by a
verifier rather than by a measurement script.

**Phase 3 — the first paper fill.** The transitive input closure of
`paper-fill-simulator` is 306 of 321 parts, so "run everything" is not the way in.
The way in is the transistor: **a part with an empty inbox produces nothing, and a
part that is off costs nothing.** So the first end-to-end run switches on the
smallest set that carries a trade from the tape to a simulated fill and into the
ledger, and leaves the rest genuinely off — which is a run the board can show
honestly, most cells dark, the live path lit.

The set is chosen from the built system, not guessed here: it is the shortest path
in the derived edge graph from `market-data` to `fill`, plus whatever those parts
refuse to run without. Parts that refuse are the finding — a part that cannot act
without an input nothing is producing is a part that has just told us what the
next phase builds.

---

## 11. What phase 2 will not do

- No live orders, no keys, no venue that can move money. Paper only (RL-005).
- No shared-memory ring. It is twice as fast on a path with 22× headroom; the
  trigger to build it is a measured saturation, and it is named here so that when
  it comes it is a decision rather than a surprise.
- No spot, no options (RL-050, RL-062).
- No `start_part` for parts outside 2b and 3. A part that does not run yet keeps
  its honest rung on the board.
- No backfill of anything the tape did not record.
