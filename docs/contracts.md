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

## R-02 — Every part has an execution switch

**Given by the user on 2026-08-20:**

> "so make sure everthin as a excecution turn on or off buttun so like in
> transistors simple turn on oand of voltae created a meraculas pat to all te
> electronoces"

Every part can be turned **off** and **on**. No exceptions, no part that is
always-on because it happens to be convenient.

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
