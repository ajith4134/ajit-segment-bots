"""Record what this machine actually holds with every part switched on.

The question RL-072 leaves open. The blueprint's target is 327 parts running, and
the plan of 2026-08-25 sized that from 59 parts running at 2.05 of 12 cores --
an extrapolation, not a measurement. On 2026-08-26 the first minute with all 327
on measured a load average of 30.6, and the governor's answer to it was to shed
sixteen parts on one reading.

So this samples the machine rather than arguing about it: how many parts stay on,
what the load does, which parts the governor sheds and how often each one comes
back, and what each running part costs in CPU seconds and resident memory read
from its own cgroup.

Every number here is read from a file this machine writes -- the heartbeat table
heartbeat-collector publishes, /proc/loadavg, /proc/meminfo, and each part's
systemd scope under the user slice. Nothing is inferred and nothing is averaged
across samples that were not taken.

    python3 measurements/2026-08-26-what-fits-on-this-box/sample_what_fits.py \
        --minutes 60 --every 60

Writes one JSON line per sample to samples.jsonl beside this file, and prints a
summary at the end. Interrupting it keeps every sample already written.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

HEARTBEAT_TABLE = Path(
    os.path.expanduser("~/.local/share/ajit-segment-bots/heartbeat-table.json")
)
USER_SLICE = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice")
SAMPLES = Path(__file__).with_name("samples.jsonl")

# The parts whose standing answers "is the governor shedding, and does anything
# come back". Read by name because this is a measurement script and not a part:
# nothing here is on the diagram, so T-4 is not in play.
GOVERNOR_PARTS = ("switching-planner", "gate-actuator", "hog-detector", "off-state-verifier")


def read_load() -> dict:
    one, five, fifteen = Path("/proc/loadavg").read_text().split()[:3]
    return {"load_1m": float(one), "load_5m": float(five), "load_15m": float(fifteen)}


def read_memory() -> dict:
    """Total minus MemAvailable, because free memory on a busy Linux box is near zero."""
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        fields[name] = int(rest.split()[0]) * 1024
    return {
        "memory_total_bytes": fields["MemTotal"],
        "memory_in_use_bytes": fields["MemTotal"] - fields["MemAvailable"],
    }


def read_part_costs() -> dict:
    """CPU seconds and resident bytes per part, from the scope the spine placed it in."""
    costs = {}
    if not USER_SLICE.is_dir():
        return costs
    for scope in USER_SLICE.iterdir():
        if not scope.name.endswith(".scope"):
            continue
        part_id = scope.name[: -len(".scope")]
        try:
            usec = 0
            for line in (scope / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec"):
                    usec = int(line.split()[1])
            resident = int((scope / "memory.current").read_text().strip())
        except (OSError, ValueError):
            # A scope that vanished between listing and reading is a part that
            # stopped, which is a fact about this sample rather than an error.
            continue
        costs[part_id] = {"cpu_seconds": usec / 1e6, "memory_bytes": resident}
    return costs


def read_heartbeats() -> dict:
    try:
        table = json.loads(HEARTBEAT_TABLE.read_text())
    except (OSError, ValueError):
        return {"table_readable": False}
    states = {}
    silent = []
    governor = {}
    for row in table["heartbeats"]:
        states[row["state"]] = states.get(row["state"], 0) + 1
        if row["state"] != "reporting":
            silent.append(row["part_id"])
        if row["part_id"] in GOVERNOR_PARTS:
            governor[row["part_id"]] = row["standing"]
    return {
        "table_readable": True,
        "table_age_seconds": (time.time_ns() - table["collected_at_ns"]) / 1e9,
        "states": states,
        "silent": sorted(silent),
        "governor": governor,
    }


def take_one_sample() -> dict:
    sample = {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    sample.update(read_load())
    sample.update(read_memory())
    sample.update(read_heartbeats())
    sample["part_costs"] = read_part_costs()
    return sample


def summarise(samples: list[dict]) -> str:
    if not samples:
        return "no samples taken"
    reporting = [s["states"].get("reporting", 0) for s in samples if s.get("table_readable")]
    loads = [s["load_1m"] for s in samples]
    silent_counts = {}
    for sample in samples:
        for part_id in sample.get("silent", []):
            silent_counts[part_id] = silent_counts.get(part_id, 0) + 1

    first, last = samples[0], samples[-1]
    lines = [
        f"samples            {len(samples)}  from {first['observed_at']} to {last['observed_at']}",
        f"parts reporting    {min(reporting)} to {max(reporting)} of 327"
        if reporting else "parts reporting    the heartbeat table was never readable",
        f"load (1m)          {min(loads):.2f} to {max(loads):.2f} on {os.cpu_count()} cores",
        f"memory in use      {last['memory_in_use_bytes'] / 2**30:.1f} of "
        f"{last['memory_total_bytes'] / 2**30:.1f} GiB at the last sample",
    ]

    if silent_counts:
        lines.append("")
        lines.append("parts off in the most samples -- the ones this box cannot hold:")
        for part_id, count in sorted(silent_counts.items(), key=lambda item: (-item[1], item[0]))[:20]:
            lines.append(f"  {count:3d}/{len(samples)}  {part_id}")

    costs = last.get("part_costs") or {}
    if costs and len(samples) > 1:
        earlier = samples[0].get("part_costs") or {}
        elapsed = max(
            time.mktime(time.strptime(last["observed_at"], "%Y-%m-%dT%H:%M:%SZ"))
            - time.mktime(time.strptime(first["observed_at"], "%Y-%m-%dT%H:%M:%SZ")),
            1.0,
        )
        burn = {
            part_id: (cost["cpu_seconds"] - earlier.get(part_id, {"cpu_seconds": 0.0})["cpu_seconds"]) / elapsed
            for part_id, cost in costs.items()
            if part_id in earlier
        }
        lines.append("")
        lines.append("cores used per part, measured across the whole run:")
        for part_id, cores in sorted(burn.items(), key=lambda item: -item[1])[:20]:
            resident = costs[part_id]["memory_bytes"] / 2**20
            lines.append(f"  {cores:6.3f} cores  {resident:7.1f} MiB  {part_id}")
        lines.append(f"  {sum(burn.values()):6.3f} cores  total across {len(burn)} parts still running")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--every", type=float, default=60.0)
    arguments = parser.parse_args()

    deadline = time.monotonic() + arguments.minutes * 60
    samples: list[dict] = []
    with SAMPLES.open("a", encoding="utf-8") as sink:
        while True:
            sample = take_one_sample()
            samples.append(sample)
            sink.write(json.dumps(sample) + "\n")
            sink.flush()
            if time.monotonic() >= deadline:
                break
            time.sleep(min(arguments.every, max(deadline - time.monotonic(), 0.0)))

    print(summarise(samples))
    print(f"\n{len(samples)} sample(s) appended to {SAMPLES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
