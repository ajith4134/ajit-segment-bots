#!/usr/bin/env python3
"""How much of the diagram is actually alive, as opposed to merely written.

    python3 dashboard/measure_diagram_coverage.py

Four numbers, and the distance between them is the whole point:

    declared    parts in the blueprint
    launchable  parts that carry a `start_part` and could be switched on
    running     parts reporting a heartbeat right now
    wired       declared wires whose producer AND consumer are both running

A part can be written, tested, contract-clean and still have never exchanged a
single message with its neighbour. `check_contracts.py` proves the *declaration*
matches the diagram; this proves the *running system* does. They are different
claims and this project has been at 100% on the first and 18% on the second.

A wire is one (producer, consumer, data type) triple implied by the blueprint's
own consumes/produces, computed the same way the contract checker computes edges
(R-01) rather than from a list someone maintains -- a list would drift, and a
drifted list would report coverage of a diagram nobody is building.

**A wire counts as carrying only when a message has travelled on it**: the
producer has published at least one of that data type and the consumer has
received at least one. Both counts are the bus's own, reported through health and
written into the heartbeat table.

That is RL-072's own wording -- a part is done when it has been observed running
*and exchanging its declared data with a real neighbour* -- and it was not what
this probe measured until 2026-08-25. Before that a wire counted when both of its
ends were alive, and the minute the last block started, the probe reported 4,993
of 4,993 wires carrying. Nothing had been measured about any of them. A number
that reaches 100% the moment every process exists is a number about processes.

The weaker reading is kept beside the real one as `wires_between_running_parts`,
because it is what the older figures in this project's history mean and a series
that silently changes definition is worse than either definition.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (str(PROJECT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


@dataclass(frozen=True)
class DiagramCoverage:
    declared_parts: int
    launchable_parts: int
    running_parts: int
    declared_wires: int
    live_wires: int
    # Wires whose ends are both running, which is what this probe called "live"
    # until 2026-08-25. Kept so the earlier figures stay comparable.
    wires_between_running_parts: int
    blocks_total: int
    blocks_fully_running: int
    blocks_dark: int
    per_block: dict
    proof: str

    @property
    def part_fraction(self) -> float:
        return self.running_parts / self.declared_parts if self.declared_parts else 0.0

    @property
    def wire_fraction(self) -> float:
        return self.live_wires / self.declared_wires if self.declared_wires else 0.0

    def as_dict(self) -> dict:
        return {
            "declared_parts": self.declared_parts,
            "launchable_parts": self.launchable_parts,
            "running_parts": self.running_parts,
            "part_fraction": self.part_fraction,
            "declared_wires": self.declared_wires,
            "live_wires": self.live_wires,
            "wire_fraction": self.wire_fraction,
            "wires_between_running_parts": self.wires_between_running_parts,
            "blocks_total": self.blocks_total,
            "blocks_fully_running": self.blocks_fully_running,
            "blocks_dark": self.blocks_dark,
            "per_block": self.per_block,
            "proof": self.proof,
        }


def read_declared_wires(features: list[dict]) -> set[tuple[str, str, str]]:
    """Every (producer, consumer, data type) the blueprint implies.

    Computed from consumes/produces exactly as R-01 requires, never enumerated. A
    part that produces a type nobody consumes contributes no wire, and a part that
    consumes a type nobody produces contributes none either -- both are real states
    of a half-built system rather than errors.
    """
    producers: dict[str, list[str]] = {}
    for feature in features:
        for data_type in feature.get("produces", ()):
            producers.setdefault(data_type, []).append(feature["id"])

    wires = set()
    for feature in features:
        for data_type in feature.get("consumes", ()):
            for producer in producers.get(data_type, ()):
                # A part reading what it writes is not a wire between parts.
                if producer != feature["id"]:
                    wires.add((producer, feature["id"], data_type))
    return wires


def read_launchable_part_ids() -> set[str]:
    """Parts carrying a `start_part`, which is what makes one switchable at all."""
    launchable = set()
    for source in (PROJECT / "parts").rglob("*.py"):
        if source.name == "__init__.py":
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        if "\ndef start_part(" in text:
            # The part id the file declares, rather than one guessed from its name.
            for line in text.splitlines():
                if line.startswith("PART_ID = "):
                    launchable.add(line.split("=", 1)[1].strip().strip('"\''))
                    break
    return launchable


def read_message_counts() -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Per part, how many messages of each type it has received and published.

    From the heartbeat table, which is where the parts' own bus counters surface.
    A table written by a collector that predates those counters simply reports
    nothing, and every wire then reads as not carrying -- which is the honest
    answer for a system nobody has measured.
    """
    from build_part_monitor import PROJECT as MONITOR_PROJECT, read_heartbeat_table_path

    project_root = str(MONITOR_PROJECT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from parts.observability.heartbeat_collector import read_heartbeat_table_file

    try:
        table = read_heartbeat_table_file(read_heartbeat_table_path())
    except Exception:  # the settings are the operator's; an unreadable table is no counts
        table = None
    received: dict[str, dict[str, int]] = {}
    published: dict[str, dict[str, int]] = {}
    for beat in (table or {}).get("heartbeats", ()):
        part_id = beat.get("part_id")
        if not part_id:
            continue
        received[part_id] = dict(beat.get("messages_received") or {})
        published[part_id] = dict(beat.get("messages_published") or {})
    return received, published


def read_running_part_ids() -> tuple[set[str], str]:
    """Parts reporting a heartbeat, from the table heartbeat-collector writes."""
    from build_part_monitor import probe_live_heartbeats

    # The same probe the part monitor uses for its RUNNING rung, so this number can
    # never disagree with the board beside it about what "running" means.
    alive, proof = probe_live_heartbeats()
    return set(alive), proof


def measure_diagram_coverage() -> DiagramCoverage:
    registry = json.loads((PROJECT / "docs" / "features.json").read_text(encoding="utf-8"))
    features = registry["features"]
    wires = read_declared_wires(features)
    launchable = read_launchable_part_ids()
    running, proof = read_running_part_ids()

    received, published = read_message_counts()
    between_running = {w for w in wires if w[0] in running and w[1] in running}
    live = {
        (producer, consumer, data_type)
        for producer, consumer, data_type in wires
        if published.get(producer, {}).get(data_type, 0) > 0
        and received.get(consumer, {}).get(data_type, 0) > 0
    }

    per_block: dict[str, dict] = {}
    for feature in features:
        block = feature.get("category", "")
        row = per_block.setdefault(block, {"declared": 0, "launchable": 0, "running": 0})
        row["declared"] += 1
        if feature["id"] in launchable:
            row["launchable"] += 1
        if feature["id"] in running:
            row["running"] += 1

    return DiagramCoverage(
        declared_parts=len(features),
        wires_between_running_parts=len(between_running),
        launchable_parts=len(launchable & {f["id"] for f in features}),
        running_parts=len(running & {f["id"] for f in features}),
        declared_wires=len(wires),
        live_wires=len(live),
        blocks_total=len(per_block),
        blocks_fully_running=sum(
            1 for r in per_block.values() if r["declared"] and r["running"] == r["declared"]
        ),
        blocks_dark=sum(1 for r in per_block.values() if r["running"] == 0),
        per_block=per_block,
        proof=proof,
    )


if __name__ == "__main__":
    coverage = measure_diagram_coverage()
    print("RL-072 — how much of the diagram is alive\n")
    print(f"  parts declared    {coverage.declared_parts:>5}")
    print(f"  parts launchable  {coverage.launchable_parts:>5}  "
          f"({coverage.launchable_parts / coverage.declared_parts:.0%} carry a start_part)")
    print(f"  parts running     {coverage.running_parts:>5}  ({coverage.part_fraction:.1%})")
    print(f"  wires declared    {coverage.declared_wires:>5}")
    print(f"  wires carrying    {coverage.live_wires:>5}  ({coverage.wire_fraction:.1%})  "
          f"a message has travelled")
    between = coverage.wires_between_running_parts
    print(f"  wires wired up    {between:>5}  "
          f"({between / coverage.declared_wires if coverage.declared_wires else 0:.1%})  "
          f"both ends running, which is the weaker claim")
    print(f"  blocks fully on   {coverage.blocks_fully_running:>5} of {coverage.blocks_total}")
    print(f"  blocks dark       {coverage.blocks_dark:>5} of {coverage.blocks_total}")
    print(f"\n  {coverage.proof}\n")
    print(f"  {'block':<28}{'running':>9}{'launchable':>12}{'declared':>10}")
    for block, row in sorted(
        coverage.per_block.items(), key=lambda kv: (-kv[1]["running"], kv[0])
    ):
        print(f"  {block:<28}{row['running']:>9}{row['launchable']:>12}{row['declared']:>10}")
