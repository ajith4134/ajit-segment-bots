"""Which parts restate a level they did not change, ranked by what it actually costs.

Two sources, and neither is enough alone. The static half reads every part file and
asks whether it publishes through `runtime/level_publishing.py`; the measured half
reads the heartbeat table the running spine writes and asks how many messages each
part published per message it received. A part can look guilty statically and be
innocent -- a detector publishing one candidate per burst is doing its job -- and a
part using the level publisher can still storm if its `identity_of` compares a field
that restates on every tick.

So the ranking is by amplification measured on the live spine, and the static answer
is a column beside it. Run it before changing a part and again after.

    python3 measurements/2026-09-04-what-burns-cpu-with-no-trades/audit_level_publishing.py

Written 2026-09-04, after `regime-classifier` was found publishing 33,535,257
`market-regime` messages from 1,190 prices received -- 69% of all traffic on the
spine, with the Indian market shut.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

PARTS = pathlib.Path(__file__).resolve().parents[2] / "parts"
HEARTBEAT = (
    pathlib.Path.home() / ".local/share/ajit-segment-bots/heartbeat-table.json"
)
LEVEL_PUBLISHERS = {"LevelPublisher", "LevelPublisherByKey", "PacedPublisher"}


def parts_publishing_through_a_level_publisher() -> dict[str, bool]:
    """Every part file, and whether it names one of the level-publishing shapes."""
    answer: dict[str, bool] = {}
    for path in sorted(PARTS.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        part_id = None
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PART_ID":
                        if isinstance(node.value, ast.Constant):
                            part_id = str(node.value.value)
            if isinstance(node, ast.Name):
                names.add(node.id)
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
        if part_id:
            answer[part_id] = bool(names & LEVEL_PUBLISHERS)
    return answer


def measured_traffic() -> dict[str, tuple[int, int]]:
    """Published and received totals per part, from the table the spine writes.

    Absent rather than zero when the table is missing: a part that has never been
    measured and a part measured at zero are different facts (Rule 8).
    """
    if not HEARTBEAT.exists():
        return {}
    table = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    traffic = {}
    for row in table.get("heartbeats", []):
        def total(field, without=()):
            value = row.get(field)
            if not isinstance(value, dict):
                return 0
            return sum(v for k, v in value.items() if k not in without)
        # `part-health` is excluded from both sides. Every part must publish it once
        # per health interval and every part consuming it receives 327 a second, so
        # counting it ranks the heartbeat rather than the storm: without this, the
        # top of the table is 300 parts that published nothing but their own health.
        traffic[row["part_id"]] = (
            total("messages_published", without=("part-health",)),
            total("messages_received", without=("part-health",)),
        )
    return traffic


def report() -> int:
    static = parts_publishing_through_a_level_publisher()
    traffic = measured_traffic()
    if not traffic:
        print(f"NOT MEASURED: no heartbeat table at {HEARTBEAT}.")
        print("The static column alone cannot rank anything -- start the spine first.")
        return 1

    rows = []
    for part_id, (published, received) in traffic.items():
        if not published:
            continue
        # Ranked by what it costs -- messages published -- not by amplification.
        # A part reading a file or the hardware receives nothing, so its ratio is
        # infinite whether it publishes twice or two million times, and sorting on
        # that puts every source at the top and every storm below them.
        rows.append((published, part_id, received, static.get(part_id)))
    rows.sort(reverse=True)

    total_published = sum(r[0] for r in rows)
    print(f"parts publishing anything but health {len(rows)}   "
          f"published {total_published:,}   received {sum(r[2] for r in rows):,}")
    print(f"part files naming a level publisher: "
          f"{sum(1 for v in static.values() if v)} of {len(static)}")
    print()
    print(f"{'part':<38}{'published':>13}{'received':>12}{'per recv':>10}  level pub")
    for published, part_id, received, uses in rows[:20]:
        uses_says = "NOT IN FILES" if uses is None else ("yes" if uses else "no")
        ratio = "no input" if not received else f"{published / received:.0f}x"
        share = f"{100 * published / total_published:.0f}%" if total_published else "-"
        print(f"{part_id:<38}{published:>13,}{received:>12,}{ratio:>10}  "
              f"{uses_says:<13}{share:>5} of all")
    return 0


if __name__ == "__main__":
    sys.exit(report())
