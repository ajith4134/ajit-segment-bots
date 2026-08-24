"""One snapshot of everything the symbol walk is judged by (RL-009).

Run it before a step and again after the system has settled, and compare the
two files. Every number is read from something that ran -- the heartbeat table
the collector writes, the tape the reader writes, the journal the recorders
write and /sys/fs/cgroup -- never asserted (Rule 8, RL-067). The rollback of
2026-08-23 (30 -> 100 -> 30, a trade decided at a fifty-six-minute-old price)
is the failure this measurement exists to catch before it costs a trade.

    .venv/bin/python measurements/2026-08-24-symbol-walk/measure_walk_step.py

Writes walk-<utc-timestamp>-<symbols>sym.json beside itself.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.settings_reader import settings_directory  # noqa: E402
from runtime.tape import read_tape_index  # noqa: E402

STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
TAPE = STATE / "tape"
SCOPES = pathlib.Path("/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice")
# How much recent tape the print rate is measured over. Long enough to smooth a
# quiet minute, short enough that the rate is about now, not about the day.
RATE_WINDOW_SECONDS = 600


def read_setting(name: str):
    import tomllib

    parsed = tomllib.loads((settings_directory() / "runtime.toml").read_text())
    return parsed[name]["value"]


def tape_print_rate() -> dict:
    """Prints per second over the last RATE_WINDOW_SECONDS, from the tape itself."""
    now_ns = time.time_ns()
    horizon_ns = now_ns - RATE_WINDOW_SECONDS * 10**9
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    per_venue: dict[str, dict] = {}
    for venue_dir in sorted(TAPE.iterdir()):
        symbols_written, records = 0, 0
        newest_ns = None
        for symbol_dir in venue_dir.iterdir():
            index = symbol_dir / f"{day}.index"
            if not index.exists():
                continue
            stamps = read_tape_index(index)["received_at_ns"]
            recent = int((stamps >= horizon_ns).sum())
            if recent:
                symbols_written += 1
                records += recent
                newest_ns = max(newest_ns or 0, int(stamps.max()))
        per_venue[venue_dir.name] = {
            "symbols_written_in_window": symbols_written,
            "records_in_window": records,
            "records_per_second": round(records / RATE_WINDOW_SECONDS, 2),
            "newest_record_age_seconds": (
                round((now_ns - newest_ns) / 1e9, 1) if newest_ns else None
            ),
        }
    return per_venue


def heartbeat_snapshot() -> dict:
    table = json.loads((STATE / "heartbeat-table.json").read_text())
    rows = table["heartbeats"]
    staleness = sorted(
        ((r["part_id"], round(r["staleness_seconds"], 2)) for r in rows),
        key=lambda item: -item[1],
    )
    return {
        "parts_reporting": len(rows),
        "table_age_seconds": round((time.time_ns() - table["collected_at_ns"]) / 1e9, 1),
        "input_loss": [
            {"part_id": r["part_id"], "loss": r["input_loss"]} for r in rows if r["input_loss"]
        ],
        "worst_staleness": staleness[:5],
        "sampler": next(r["standing"] for r in rows if r["part_id"] == "price-level-sampler"),
        "planner": next(r["standing"] for r in rows if r["part_id"] == "switching-planner"),
        "actuator": next(r["standing"] for r in rows if r["part_id"] == "gate-actuator"),
        "hogs": next(r["standing"] for r in rows if r["part_id"] == "hog-detector"),
        "forecaster": next(
            r["standing"] for r in rows if r["part_id"] == "memory-pressure-forecaster"
        ),
    }


def scope_usage() -> dict:
    """What every part actually costs right now, read from its own cgroup."""
    parts = {}
    for scope in sorted(SCOPES.glob("*.scope")):
        entry = {}
        for filename, key in (("memory.current", "memory_bytes"), ("memory.peak", "memory_peak_bytes")):
            try:
                entry[key] = int((scope / filename).read_text())
            except (OSError, ValueError):
                entry[key] = None
        try:
            pressure = (scope / "cpu.pressure").read_text().splitlines()[0]
            entry["cpu_pressure_some_avg60"] = float(pressure.split("avg60=")[1].split()[0])
        except (OSError, IndexError, ValueError):
            entry["cpu_pressure_some_avg60"] = None
        parts[scope.name.removesuffix(".scope")] = entry
    ranked = sorted(parts.items(), key=lambda kv: -(kv[1]["memory_bytes"] or 0))
    return {
        "scopes": len(parts),
        "total_memory_bytes": sum(p["memory_bytes"] or 0 for p in parts.values()),
        "top_memory": [
            {"part_id": name, "memory_mb": round((entry["memory_bytes"] or 0) / 2**20, 1)}
            for name, entry in ranked[:8]
        ],
    }


def snapshot() -> dict:
    return {
        "measured_at_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "captured_symbol_count": read_setting("captured_symbol_count"),
        "tape": tape_print_rate(),
        "heartbeats": heartbeat_snapshot(),
        "scopes": scope_usage(),
    }


if __name__ == "__main__":
    taken = snapshot()
    stamp = taken["measured_at_utc"].replace(":", "").replace("-", "").replace("+0000", "Z")
    out = pathlib.Path(__file__).parent / f"walk-{stamp}-{taken['captured_symbol_count']}sym.json"
    out.write_text(json.dumps(taken, indent=1) + "\n")
    print(f"wrote {out}")
    print(json.dumps({k: taken[k] for k in ("captured_symbol_count", "tape")}, indent=1))
