#!/usr/bin/env python3
"""Per foundational feature, which of its parts' declared wires actually carry.

    python3 dashboard/audit_feature_dataflow.py                 every feature
    python3 dashboard/audit_feature_dataflow.py market-data-feed one feature, part by part
    python3 dashboard/audit_feature_dataflow.py --json           machine-readable

This is the measuring instrument for the audit temporary goal of 2026-09-05:
walk all 29 foundational features one at a time and prove, per part and in both
directions, that data is *moving* on each declared connection -- not that the
contract declares it should.

`check_contracts.py` proves the declaration is coherent (R-01).
`measure_diagram_coverage.py` reports one whole-system number (RL-072).
Neither answers "which wire of which part in this feature is dead", which is
the question the audit is walked with, so this reports exactly that.

What it measures, and where every number comes from:

    the blueprint            docs/features.json -- consumes/produces per part
    the wires                computed from those, the same way R-01 computes edges
    the counters             each part's own bus counts, surfaced through health
                             into the heartbeat table heartbeat-collector writes

A part that is not running reports no counters, so every one of its wires reads
as not carrying. That is the honest answer and not a fault of the probe: a part
that is off is a part exchanging nothing.

Three states per wire, because two would lie (Rule 8):

    CARRYING       producer published this type AND consumer received it
    NOT CARRYING   both ends running, and no message has travelled
    NOT MEASURED   an end is not running, so nothing was observed at all

The difference between the last two matters more than anything else here: an
idle wire between two live parts is a finding about the system, while a wire
whose end is off is a finding about what is switched on.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (str(PROJECT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from measure_diagram_coverage import (  # noqa: E402  (path set above)
    read_declared_wires,
    read_launchable_part_ids,
    read_message_counts,
    read_running_part_ids,
)

CARRYING = "CARRYING"
NOT_CARRYING = "NOT CARRYING"
NOT_MEASURED = "NOT MEASURED"


@dataclass(frozen=True)
class WireReading:
    """One (producer, consumer, data type) wire, and what was observed on it."""

    producer: str
    consumer: str
    data_type: str
    verdict: str
    published: int
    received: int


@dataclass
class PartAudit:
    """One part's own reading: what reached it, and what left it."""

    part_id: str
    name: str
    is_launchable: bool
    is_running: bool
    consumed_types: dict[str, str] = field(default_factory=dict)
    produced_types: dict[str, str] = field(default_factory=dict)
    unproduced_consumes: list[str] = field(default_factory=list)
    unconsumed_produces: list[str] = field(default_factory=list)

    @property
    def inputs_carrying(self) -> int:
        return sum(1 for v in self.consumed_types.values() if v == CARRYING)

    @property
    def outputs_carrying(self) -> int:
        return sum(1 for v in self.produced_types.values() if v == CARRYING)


@dataclass
class FeatureAudit:
    """One of the 29 foundational features, walked part by part."""

    feature_id: str
    name: str
    parts: list[PartAudit]
    wires: list[WireReading]

    @property
    def parts_running(self) -> int:
        return sum(1 for p in self.parts if p.is_running)

    @property
    def parts_launchable(self) -> int:
        return sum(1 for p in self.parts if p.is_launchable)

    @property
    def wires_carrying(self) -> int:
        return sum(1 for w in self.wires if w.verdict == CARRYING)

    @property
    def wires_idle(self) -> int:
        return sum(1 for w in self.wires if w.verdict == NOT_CARRYING)

    @property
    def wires_unmeasured(self) -> int:
        return sum(1 for w in self.wires if w.verdict == NOT_MEASURED)


def judge_wire(
    producer: str,
    consumer: str,
    data_type: str,
    running: set[str],
    published: dict[str, dict[str, int]],
    received: dict[str, dict[str, int]],
) -> WireReading:
    """What one wire is doing, distinguishing idle from never observed."""
    sent = published.get(producer, {}).get(data_type, 0)
    got = received.get(consumer, {}).get(data_type, 0)
    if producer not in running or consumer not in running:
        verdict = NOT_MEASURED
    elif sent > 0 and got > 0:
        verdict = CARRYING
    else:
        verdict = NOT_CARRYING
    return WireReading(producer, consumer, data_type, verdict, sent, got)


def summarise_type(verdicts: list[str]) -> str:
    """One data type may ride several wires; the best any of them managed."""
    if not verdicts:
        return NOT_MEASURED
    if CARRYING in verdicts:
        return CARRYING
    if NOT_CARRYING in verdicts:
        return NOT_CARRYING
    return NOT_MEASURED


def audit_every_feature() -> list[FeatureAudit]:
    registry = json.loads((PROJECT / "docs" / "features.json").read_text(encoding="utf-8"))
    features = registry["features"]
    categories = {c["id"]: c["name"] for c in registry["categories"]}

    wires = read_declared_wires(features)
    launchable = read_launchable_part_ids()
    running, _proof = read_running_part_ids()
    received, published = read_message_counts()

    readings = [judge_wire(p, c, t, running, published, received) for p, c, t in sorted(wires)]
    into: dict[str, dict[str, list[str]]] = {}
    out_of: dict[str, dict[str, list[str]]] = {}
    for reading in readings:
        into.setdefault(reading.consumer, {}).setdefault(reading.data_type, []).append(
            reading.verdict
        )
        out_of.setdefault(reading.producer, {}).setdefault(reading.data_type, []).append(
            reading.verdict
        )

    by_category: dict[str, list[PartAudit]] = {}
    for feature in features:
        part_id = feature["id"]
        audit = PartAudit(
            part_id=part_id,
            name=feature.get("name", part_id),
            is_launchable=part_id in launchable,
            is_running=part_id in running,
        )
        for data_type in feature.get("consumes", ()):
            verdicts = into.get(part_id, {}).get(data_type)
            if verdicts is None:
                # Nobody in the blueprint produces this type at all.
                audit.unproduced_consumes.append(data_type)
                continue
            audit.consumed_types[data_type] = summarise_type(verdicts)
        for data_type in feature.get("produces", ()):
            verdicts = out_of.get(part_id, {}).get(data_type)
            if verdicts is None:
                audit.unconsumed_produces.append(data_type)
                continue
            audit.produced_types[data_type] = summarise_type(verdicts)
        by_category.setdefault(feature["category"], []).append(audit)

    audits = []
    for category, parts in by_category.items():
        member_ids = {p.part_id for p in parts}
        touching = [w for w in readings if w.producer in member_ids or w.consumer in member_ids]
        audits.append(
            FeatureAudit(
                feature_id=category,
                name=categories.get(category, category),
                parts=sorted(parts, key=lambda p: p.part_id),
                wires=touching,
            )
        )
    return sorted(audits, key=lambda a: a.feature_id)


def print_feature_summary(audits: list[FeatureAudit]) -> None:
    print("Foundational features — how much of each one's declared flow is moving\n")
    header = f"  {'feature':<26}{'parts on':>10}{'carrying':>10}{'idle':>8}{'unmeasured':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for audit in audits:
        parts = f"{audit.parts_running}/{len(audit.parts)}"
        print(
            f"  {audit.feature_id:<26}{parts:>10}{audit.wires_carrying:>10}"
            f"{audit.wires_idle:>8}{audit.wires_unmeasured:>12}"
        )
    print(
        f"\n  {len(audits)} features, "
        f"{sum(len(a.parts) for a in audits)} parts, "
        f"{sum(len(a.wires) for a in audits)} feature-touching wire readings "
        "(a wire between two features is read by both)."
    )


def print_one_feature(audit: FeatureAudit) -> None:
    print(f"{audit.name}  ({audit.feature_id})\n")
    print(
        f"  parts   {audit.parts_running} running, "
        f"{audit.parts_launchable} launchable, {len(audit.parts)} declared"
    )
    print(
        f"  wires   {audit.wires_carrying} carrying, "
        f"{audit.wires_idle} idle between running parts, "
        f"{audit.wires_unmeasured} not measured (an end is off)\n"
    )
    for part in audit.parts:
        if part.is_running:
            state = "running"
        elif part.is_launchable:
            state = "OFF"
        else:
            state = "NO start_part"
        inputs = f"{part.inputs_carrying}/{len(part.consumed_types)}"
        outputs = f"{part.outputs_carrying}/{len(part.produced_types)}"
        print(f"  {part.part_id:<38}in {inputs:>7}   out {outputs:>7}   {state}")
        for label, types in (("consumes", part.consumed_types), ("produces", part.produced_types)):
            dead = sorted(name for name, verdict in types.items() if verdict != CARRYING)
            if dead:
                print(f"      {label} not carrying: {', '.join(dead)}")
        if part.unproduced_consumes:
            print(
                "      consumes a type NO part produces: "
                + ", ".join(sorted(part.unproduced_consumes))
            )
        if part.unconsumed_produces:
            print(
                "      produces a type NO part consumes: "
                + ", ".join(sorted(part.unconsumed_produces))
            )


def as_dict(audit: FeatureAudit) -> dict:
    return {
        "feature_id": audit.feature_id,
        "name": audit.name,
        "parts_running": audit.parts_running,
        "parts_launchable": audit.parts_launchable,
        "parts_declared": len(audit.parts),
        "wires_carrying": audit.wires_carrying,
        "wires_idle": audit.wires_idle,
        "wires_unmeasured": audit.wires_unmeasured,
        "parts": [
            {
                "part_id": part.part_id,
                "running": part.is_running,
                "launchable": part.is_launchable,
                "consumes": part.consumed_types,
                "produces": part.produced_types,
                "consumes_nobody_produces": sorted(part.unproduced_consumes),
                "produces_nobody_consumes": sorted(part.unconsumed_produces),
            }
            for part in audit.parts
        ],
    }


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("--")]
    audits = audit_every_feature()
    if wanted:
        known = {a.feature_id for a in audits}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            print(f"no such foundational feature: {', '.join(unknown)}", file=sys.stderr)
            print("known: " + ", ".join(sorted(known)), file=sys.stderr)
            return 2
        audits = [a for a in audits if a.feature_id in wanted]
    if "--json" in argv:
        print(json.dumps([as_dict(a) for a in audits], indent=2))
        return 0
    if wanted:
        for index, audit in enumerate(audits):
            if index:
                print()
            print_one_feature(audit)
    else:
        print_feature_summary(audits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
