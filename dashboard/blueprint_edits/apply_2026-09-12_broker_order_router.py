#!/usr/bin/env python3
"""Declare broker-order-router, the Indian replacement for ccxt-order-router.

docs/proposals/the-broker-order-router.md: UpstoxAdapter has had
order_endpoint_url, build_order_request_payload and read_order_result since the
cutover and no part ever called any of them, so the live path on this market was
unreachable. The paper book filled everything and reported healthy, which is why
nobody noticed.

Produces `raw-venue-order-status`, the same type the crypto router produces, so
venue-order-status-translator, order-reject-classifier,
order-not-found-debouncer and clock-skew-monitor all keep working unchanged.

Idempotent: re-running changes nothing once applied.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/the-broker-order-router.md"
TODAY = "2026-09-12"
PART = "broker-order-router"

d = json.loads(REG.read_text())
if any(f["id"] == PART for f in d["features"]):
    print(f"already applied: {PART} is declared")
    raise SystemExit(0)

anchor = next(f for f in d["features"] if f["id"] == "broker-margin-quoter")

d["features"].append({
    "id": PART,
    "name": "Broker order router",
    "role": "place each live order with the broker",
    "category": anchor["category"],
    "consumes": ["order-request", "broker-token-standing", "money-mode", "symbol-universe"],
    "produces": ["raw-venue-order-status", "part-health"],
    "switchable": True, "off_releases_resources": True,
    "states": ["off", "on"], "origin": "proposed", "proposed": TODAY,
    "evidence": PROPOSAL,
    "resource_class": "io-bound", "rate_risk": "changes-the-answer",
    "skipped_tick_effect": "delays",
})

REG.write_text(json.dumps(d, indent=1) + "\n")
print(f"declared {PART}: consumes 4, produces raw-venue-order-status")
