#!/usr/bin/env python3
"""context-assembler consumes retrieval-query.

Its jobs are built from retrieval hits alone, so a request whose retrieval found
nothing relevant never becomes a context and its prompt is refused for missing
context -- measured live 2026-09-16: 75 queries, 67 with nothing above the
similarity floor, 0 contexts, 901 of 904 prompts refused. A query is the evidence
that a request wants context at all, and the assembler already keys everything by
the query id both a hit and a query carry.
See docs/proposals/a-prompt-can-be-assembled-with-no-retrieved-passage.md.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "context-assembler"
NEEDED = "retrieval-query"

d = json.loads(REG.read_text())
feature = next((f for f in d["features"] if f["id"] == PART), None)
if feature is None:
    raise SystemExit(f"{PART} is not declared")
if NEEDED in feature["consumes"]:
    print(f"already applied: {PART} already consumes {NEEDED}")
    raise SystemExit(0)

# Inserted beside the type it belongs with rather than sorted: only 170 of the
# 375 features carry a sorted `consumes`, so the order is the author's and a sort
# would rewrite it -- and the part's own declaration has to equal this list
# exactly (RL-067).
consumes = list(feature["consumes"])
consumes.insert(consumes.index("retrieval-hit") + 1, NEEDED)
feature["consumes"] = consumes
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART} now consumes {', '.join(feature['consumes'])}")
