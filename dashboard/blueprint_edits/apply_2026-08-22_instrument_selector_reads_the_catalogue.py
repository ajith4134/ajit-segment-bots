#!/usr/bin/env python3
"""instrument-selector consumes symbol-universe. Proposed by Claude 2026-08-22.

Rationale: docs/proposals/venue-declared-funding-facts.md. Idempotent.

Why the blueprint has to change at all: the selector refuses a perpetual whose
carry it cannot price, a perpetual's carry is its funding rate times the
settlements inside the intent's horizon, and nothing published either number. It
was registering the perpetual from the fact that the symbol printed a trade --
an inference it documented in its own docstring -- because no type reaching it
carried a catalogue.

`symbol-universe` is the venue's own listing, and venue-declared contract facts
already travel on it: `price_increment` rides there and `tick-size-resolver`
reads it. Funding is the same class of fact about the same contract, out of the
same response. So the selector reads the listing instead of inferring it, which
is one edge and no new part.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/venue-declared-funding-facts.md"

PART_ID = "instrument-selector"
ADDED_INPUT = "symbol-universe"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

part = features.get(PART_ID)
if part is None:
    raise SystemExit(f"{PART_ID} is not in the blueprint; this edit changes an existing part")

consumes = list(part["consumes"])
if ADDED_INPUT not in consumes:
    consumes.append(ADDED_INPUT)
    part["consumes"] = consumes

# `evidence` is deliberately not written. It names what proposed the *part*, and
# this proposal did not -- it proposed one of its inputs. Overwriting it would put
# a 2026-08-22 document's name on a part proposed on 2026-08-20, which is the kind
# of quiet drift that makes provenance stop being worth reading. The record of this
# edge is this script and PROPOSAL beside it.

REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
print(f"{PART_ID}: consumes {', '.join(part['consumes'])}")
