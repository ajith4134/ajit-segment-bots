# Contract rules — ajit-segment-bots

Rules that bind **every** part, in every category, forever. A category says what
a part is about. These say how a part must behave to be allowed in the building.

They exist because of the user's hard constraint: however many parts are added,
the flow between them must not become inconsistent or messy. A rule here is the
mechanism that makes that true rather than hoped for, so each one is checked by
`dashboard/render_blueprint.py` and any breach shows on the board as a failing
state — never as a silent gap.

---

## R-01 — A part names data, never another part

A feature declares only the data it **consumes** and the data it **produces**. It
never names another feature.

Two parts are connected when, and only when, one produces a data type the other
consumes. The edges in the diagram are computed from that, never drawn by hand.

**What this buys:**

- **Replaceable parts.** Swap any part for a better one honouring the same
  consumes/produces contract, and nothing else in the system changes. This is
  what makes a part a spare part.
- **No private wiring.** A new part cannot run a secret cable to an old part,
  because parts do not know each other's names. It can only attach to data that
  already exists, or declare data of its own.
- **Mess is detectable.** Because the flow is declared rather than implied, it
  can be checked.

**Checked:** dangling input (consumes what nothing produces), orphan output
(produces what nothing consumes), isolated part (neither reads nor feeds
anything), undeclared data type, and a part belonging to no category.

---

## R-02 — Every part is a transistor

**The full rule lives in [`transistor-rule.md`](transistor-rule.md), and it
decides how every feature in this project is built.** What follows is the summary
only; that file is the authority.

T-1 every feature is the same shape · T-2 control path separate from data path ·
T-3 off means genuinely off · T-4 a part knows nothing about the circuit ·
T-5 states are explicit and countable · T-6 grow by adding parts, never by making
a part cleverer.

**Checked:** `dashboard/check_contracts.py`, run by the git pre-commit hook, which
refuses a commit that breaks any of them.

**Given by the user on 2026-08-20:**

> "so make sure everthin as a excecution turn on or off buttun so like in
> transistors simple turn on oand of voltae created a meraculas pat to all te
> electronoces"

Every part can be turned **off** and **on**. No exceptions, no part that is
always-on because it happens to be convenient.

---

## R-03 — Peer blocks never wire into each other

Blocks that carry the same `peer_group` in `docs/features.json` are copies of one
idea that run separately (RL-048: bull bot, bear bot, profit tailgating bot form
`segment-bots`). A part in one of them never produces a data type a part in a
sibling consumes. Each bot's internal data is its own type —
`bull-feature-vector`, `bear-feature-vector` — and only the deliberate merge
point, `directional-opinion`, is shared.

**Why it exists:** on 2026-08-20 bull and bear were stamped from one template with
the same type ids, and because edges are computed from types (R-01) the
derivation drew 46 wires between the two bots that nobody had designed. R-01
alone cannot see that: every one of those wires was a produced type meeting a
consumed type. This rule does.

**Checked:** every derived edge whose producer and consumer sit in different
blocks of one `peer_group` is a violation.

The reasoning is the transistor: a single switch does almost nothing, but the
composition of many simple on/off gates is what produced all of electronics. The
same is intended here — the system's behaviour comes from which parts are live at
a given moment, not from any one part being clever.

**What this buys:**

- The resource governor (C-11) can actually govern. It cannot turn off a hog that
  has no off switch.
- A failing part can be isolated without taking the building down.
- A new part can be introduced dark, switched on when it earns it, and switched
  off again without a rewrite.

**Checked:** a feature that does not declare `"switchable": true` is a contract
violation.

**Open, to settle when C-11 is detailed:** how the governor learns a part's
resource appetite — declared by the part, or measured at runtime. Not decided,
and not defaulted.

---

## Rules deliberately not written yet

The flow between categories — which category feeds which — is **not** declared.
Until the user says it, the diagram shows blocks without arrows. An inferred
arrow would be read back later as a decision that was made.

---

## How edges are counted — health is not flow

*Added 2026-08-20, after measuring.*

At 71 parts the board reported **793 edges**. Measured, **630 of those (79.4%) were
`part-health`** and only **163 were design flow**.

That is arithmetic, not a defect in the parts: every part emits health and a handful
of parts read it, so the type fans out as *producers × readers* and grows with the
square of the system while the trade flow grows linearly. Adding nine parts to C-21
added twenty-four data edges and three hundred health edges.

The diagrams were always right — every renderer already excluded `part-health`, and
it has its own control-plane picture. **The number was the problem.** A single "793
edges" made the flow look five times denser than it is, and hid the one figure worth
watching as parts are added: the 163.

So the counts are split, and neither number hides the other:

    Architecture blueprint    71 parts, 163 data edges, 630 health
    Flow contract             163 data edges, all resolved (630 health emissions, counted apart)

**Why this matters beyond tidiness.** The user's hard constraint is that however many
parts go in, the flow between them stays legible. A metric that folds telemetry into
flow would have reported that constraint as *degrading* — 164 → 424 → 737 → 793 —
when the actual design flow went 68 → 106 → 137 → 163. The board would have raised an
alarm about the wrong thing while the real number stayed healthy.

Rule 8 again, and the same shape as the rest of it: a display shows measured state.
Counting two different things as one is how a true number becomes a false statement.
