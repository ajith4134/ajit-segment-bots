#!/usr/bin/env python3
"""exposure-limiter consumes account-balance, so its caps are fractions of something.

Proposed by Claude 2026-08-25 during the payload-shape sweep.
Rationale: docs/proposals/an-exposure-limiter-that-never-limited.md
Idempotent.

Every cap this part applies is a fraction of the segment's allotment, and the only
input it had -- `position` -- carries a quantity and an entry price, never a
fraction. It read one through a getattr default of 0.0, so every position was
observed at zero exposure and the limit it published was the full per-position cap
on every tick since it first ran.

The allotment lives on `account-balance`, which paper-account-keeper produces and
three other parts already consume for the same reason.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/an-exposure-limiter-that-never-limited.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

LIMITER = "exposure-limiter"
BALANCE = "account-balance"
PRODUCER = "paper-account-keeper"

limiter = features.get(LIMITER)
if limiter is None:
    raise SystemExit(f"{LIMITER} is not in the registry; this edit is out of date")

producer = features.get(PRODUCER)
if producer is None or BALANCE not in producer.get("produces", ()):
    raise SystemExit(
        f"{PRODUCER} no longer produces {BALANCE}, so this edit would give {LIMITER} "
        f"a wire to nothing. Amend {PROPOSAL} rather than letting it create a "
        f"dangling edge."
    )

changed = []
if BALANCE not in limiter["consumes"]:
    # Appended rather than sorted: a part's declaration in code carries the order
    # its author wrote, and the two are compared for equality by the wiring check.
    limiter["consumes"] = list(limiter["consumes"]) + [BALANCE]
    changed.append(f"{LIMITER}: consumes {BALANCE}")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
