#!/usr/bin/env python3
"""broker-news-reader consumes broker-token-standing too.

Its declaration said it consumes only `broker-instrument-listing`, which is
where it learns the instrument keys to ask about. But Upstox's News API is
AUTHENTICATED -- `Authorization: Bearer {access_token}`, verified against
upstox.com/developer/api-documentation/get-news on 2026-09-12 -- so a reader
with the listings and no token can name what to ask for and cannot ask.

The gap was invisible while the part had no source file: a declaration nothing
implements is never wrong about what it needs.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PART = "broker-news-reader"
NEEDED = "broker-token-standing"

d = json.loads(REG.read_text())
feature = next((f for f in d["features"] if f["id"] == PART), None)
if feature is None:
    raise SystemExit(f"{PART} is not declared")
if NEEDED in feature["consumes"]:
    print(f"already applied: {PART} already consumes {NEEDED}")
    raise SystemExit(0)

feature["consumes"] = sorted(set(feature["consumes"]) | {NEEDED})
REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"{PART} now consumes {', '.join(feature['consumes'])}")
