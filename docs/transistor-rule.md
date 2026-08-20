# The transistor rule

**This decides how every feature in this project is built.** It is not a
guideline and it is not a property of one category. It is the shape of the part.

Given by the user on 2026-08-20:

> "so make sure everthin as a excecution turn on or off buttun so like in
> transistors simple turn on oand of voltae created a meraculas pat to all te
> electronoces"

and reinforced:

> "now make sure to understand my transitor analoy fully becaue it desised ow all
> te featues are build so make sure you understand and make it a rule it folloes
> wit out me remaindin"

---

## What the analogy actually says

A transistor is not impressive. It is a switch with no moving parts. Everything
electronic that has ever been built came from composing billions of them, and it
worked *because* the part is simple and identical, not despite it.

The mistake would be to read this as "add an on/off button to each feature". The
button is the smallest of the six things the analogy demands. Taken properly, it
says: **stop building features, start building transistors.**

---

## T-1 — Every feature is the same shape

A transistor is interchangeable with every other transistor. That uniformity is
what allows any one to drive any other, and it is the entire basis of
composition. There is no such thing as a special transistor that needs handling.

**So:** every feature is built to one part template — the same lifecycle, the
same declarations, the same way of being addressed. There is no privileged
feature, no feature that is "core" and exempt, no feature with a bespoke
interface because it happened to be written first.

*Machine-checked:* a feature missing any required field is a violation.

---

## T-2 — The control path is separate from the data path

A transistor has three terminals, and this is the part most people skip: current
flows source to drain, but the **gate** is a separate terminal. What flows
through the part and what switches the part are different wires.

**So:** the system has two planes that never mix.

- **Data plane** — what a part consumes and produces (R-01). Parts are connected
  here by data type, and never by name.
- **Control plane** — on, off, and the part's state. Only the resource governor
  (C-11) drives this.

A feature never turns another feature on or off. Not directly, not by sending a
message, not as a side effect. If part A needs part B running, that is the
governor's problem, expressed through the control plane — because the moment a
feature can switch another feature, the flow the user demanded stay clean has a
second, invisible graph running underneath it.

*Machine-checked:* a feature declaring control over another feature is a violation.

---

## T-3 — Off means genuinely off

A transistor that is off draws leakage and nothing else. That is the only reason
billions fit on a chip — you pay for what is switching, not for what exists.

**So:** an off part releases its CPU and its RAM. It is not a paused thread, not
a loop that sleeps, not an object holding its buffers "so restart is fast". If
off still costs, the governor cannot govern — it would be turning things off and
finding the machine just as full, which is exactly the queueing the user said
must not happen.

*Machine-checked:* a feature must declare that off releases its resources.
*Later, measurable:* the governor observes real CPU and RAM before and after.

---

## T-4 — A part knows nothing about the circuit

No transistor in a processor knows it is in a processor. It has no model of the
adder it belongs to, and it behaves identically if you move it into a radio.

**So:** a feature holds no knowledge of the system's topology, of which other
features exist, or of what it is contributing to. This is R-01 seen from the
other side, and it is what makes a part genuinely replaceable — a part that knows
its neighbours cannot be swapped without teaching the replacement the same
gossip.

*Machine-checked:* a feature naming another feature is a violation.

---

## T-5 — States are explicit and countable

A gate is on or it is off. There is no "mostly conducting". The determinism is
the point: you can reason about a billion of them precisely because each one is
in one of a small number of known states.

**So:** every part is in exactly one state drawn from a declared, closed set.
There is no "degraded but sort of working", no implicit half-started. If a new
state is genuinely needed, it is named and added to the set, never smuggled in as
a flag.

*Machine-checked:* a feature must declare its states, and each must come from the
declared state vocabulary.

---

## T-6 — Grow by adding parts, never by making a part cleverer

Nobody ever built a better circuit by improving a transistor. They added more and
rewired them. The part stayed dumb; the arrangement got smart.

**So:** new capability comes from adding parts and connecting data types — never
from editing an existing part's internals so it can also do a second job. A part
that has grown a second responsibility is not an improved part, it is two parts
that have been welded together, and the weld is where the mess the user warned
about begins.

This is also what makes "replace a part with a higher version" real: the
replacement honours the same contract and does the same one job better.

*Review-checked:* a part's role is one sentence naming one responsibility. A role
sentence containing "and" is the usual first symptom.

---

## Why the switch alone was not enough

The user's first statement gave the switch. Read narrowly, that produces a system
of ordinary features that happen to have an off button — and the governor would
still be unable to work, because off parts would still hold memory (T-3), parts
would still switch each other behind its back (T-2), and no two parts would be
alike enough to swap (T-1).

The switch is the visible end of the idea. The six rules above are the idea.

---

## How this holds without being asked

1. **`CLAUDE.md` in this project points here**, so it loads at the start of every
   session in this directory rather than depending on anyone's memory.
2. **`dashboard/check_contracts.py`** enforces the machine-checkable parts and
   exits non-zero on any breach.
3. **A git pre-commit hook** runs that check, so a commit carrying a violation is
   refused rather than discussed.
4. **The status board** renders every breach in red, with the rule it broke.
