"""Start capturing one venue's trades to the tape, now, and keep capturing.

    .venv/bin/python operate/start_trade_capture.py binance-usdm

**This is an operations entry point, not a part.** It does two jobs that belong
to parts which do not exist yet, and it says so rather than pretending otherwise:

- `stream-budget-planner` will produce `stream-plan`. Until it does, this builds
  one from the symbol catalogue and the adapter's own fit question.
- the resource governor will fork, place and switch parts. Until it does, this
  runs one part in the foreground with a control socket nothing else holds, so
  the only way to switch it off is to kill it -- which is the ordinary way a part
  is switched off anyway (phase 0 §4), and the tape survives it by construction.

It exists because of the one fact that orders this whole phase: history accrues
only in real time. Every hour spent building the governor first is an hour of
market that no later phase can recover. So the tape starts on what is built, and
the parts that will take these two jobs over replace this script rather than
extending it.

Everything it decides comes from settings with provenance (RL-061) or from the
adapter's declared venue facts. Nothing here is a number chosen at the point of
use.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import time

PROJECT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

# Before anything imports numpy, which the tape does. Measured in phase 0: with
# these unset, `import numpy` alone puts 12 kernel threads in the process.
from runtime.forkserver_launcher import apply_blas_thread_caps  # noqa: E402

apply_blas_thread_caps()

from parts.market_data_feed.symbol_catalogue_reader import (  # noqa: E402
    SymbolCatalogueReader,
    describe_catalogue,
)
from parts.market_data_feed.venue_trade_stream_reader import (  # noqa: E402
    CAPTURED_STREAM_KIND,
    TradeStreamReader,
    describe_capture,
)
from runtime.control_channel import create_control_socket_pair  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402
from runtime.stream_plan import ConnectionAssignment, StreamPlan  # noqa: E402
from runtime.venues.adapter_registry import (  # noqa: E402
    load_captured_venue_adapters,
    load_venue_adapter,
)
from runtime.venues.venue_adapter import StreamRequest  # noqa: E402

# How long to wait on a REST call before treating it as a failure. Not a venue
# limit -- it is how long this script is willing to sit in a syscall before
# saying so, and both venues answer their catalogue in well under a second.
CATALOGUE_REQUEST_TIMEOUT_SECONDS = 30.0


def read_runtime_settings():
    return load_settings_document(settings_directory() / "runtime.toml", "runtime")


def expand_tape_root(configured: str) -> pathlib.Path:
    """The operator writes `~/...`; nothing else in the runtime expands it."""
    return pathlib.Path(configured).expanduser()


def build_trade_plan(adapter, settings) -> tuple[StreamPlan, dict]:
    """Read the venue's catalogue, select symbols, and pack them onto connections.

    The packing asks the adapter whether one more fits and opens another
    connection when it does not. That question has a different answer per venue --
    a stream count on one, a character count of the serialised subscribe payload
    on the other -- which is exactly why it is asked rather than computed here.
    """
    reader = SymbolCatalogueReader(
        adapter=adapter,
        captured_symbol_count=settings.read_value("captured_symbol_count"),
        selection_metric=settings.read_value("symbol_selection_metric"),
        request_timeout_seconds=CATALOGUE_REQUEST_TIMEOUT_SECONDS,
    )
    selection = reader.read_catalogue()
    if not selection:
        raise SystemExit(
            f"{adapter.venue_id} returned no capturable symbols: "
            f"{reader.standing.last_failure or 'the catalogue was empty'}"
        )

    assignments: list[ConnectionAssignment] = []
    current: list[StreamRequest] = []
    topics: list[str] = []
    for entry in selection:
        request = StreamRequest(stream_kind=CAPTURED_STREAM_KIND, symbol=entry.symbol)
        topic = adapter.subscription_topic(request)
        if current and not adapter.does_topic_fit_connection(topics, topic):
            assignments.append(
                ConnectionAssignment(venue_id=adapter.venue_id, requests=tuple(current))
            )
            current, topics = [], []
        current.append(request)
        topics.append(topic)
    if current:
        assignments.append(ConnectionAssignment(venue_id=adapter.venue_id, requests=tuple(current)))

    return StreamPlan(connections=tuple(assignments)), describe_catalogue(reader)


def health_log_path(tape_root: pathlib.Path, venue_id: str) -> pathlib.Path:
    """Where this capture writes what it measured about itself.

    Beside the tape rather than inside it: the tape is what the venue said, and
    this is what we observed about our own reading of it. Mixing the two would
    put our own reports into the record we promise is the venue's.
    """
    path = tape_root.parent / "capture-health" / f"{venue_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    settings = read_runtime_settings()
    known = [adapter.venue_id for adapter in load_captured_venue_adapters(settings)]
    parser.add_argument("venue", choices=known, help="which captured venue to read")
    arguments = parser.parse_args(argv)

    adapter = load_venue_adapter(arguments.venue)
    tape_root = expand_tape_root(settings.read_value("tape_root"))
    plan, catalogue = build_trade_plan(adapter, settings)

    reader = TradeStreamReader(
        adapter=adapter,
        plan=plan,
        tape_root=tape_root,
        writeback_interval_bytes=settings.read_value("writeback_interval"),
        reconnect_backoff_floor_seconds=settings.read_value("venue_reconnect_backoff_floor"),
        reconnect_backoff_ceiling_seconds=settings.read_value("venue_reconnect_backoff_ceiling"),
        drain_interval_seconds=settings.read_value("stream_drain_interval"),
    )

    log_path = health_log_path(tape_root, adapter.venue_id)
    health_interval = settings.read_value("part_health_interval")
    started_at = time.time_ns()

    def record(event: dict) -> None:
        """Append one measured observation. Flushed, because a killed part is normal."""
        event = {"observed_at_ns": time.time_ns(), "pid": os.getpid(), **event}
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
            handle.flush()

    record(
        {
            "event": "capture-started",
            "venue_id": adapter.venue_id,
            "tape_root": str(tape_root),
            "connections": len(plan.connections),
            "symbols": sum(len(c.requests) for c in plan.connections),
            "catalogue": catalogue,
            "trade_fidelity": str(adapter.trade_fidelity),
        }
    )

    # A control socket nothing else holds. run_part selects on it, so the loop
    # keeps its shape for the governor that will hold the other end -- and until
    # then, SIGKILL and SIGTERM are the switch.
    governor_end, part_end = create_control_socket_pair()
    stopping = {"requested": False}

    def stop(signal_number, _frame):
        stopping["requested"] = True
        record({"event": "signalled", "signal": signal_number})
        governor_end.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_report = [0.0]

    def tick() -> None:
        reader.capture_one_tick()
        now = time.monotonic()
        if now - last_report[0] >= health_interval:
            record({"event": "capture-standing", **describe_capture(reader)})
            last_report[0] = now

    from parts.market_data_feed.venue_trade_stream_reader import PART_DECLARATION
    from runtime.part_process import run_part

    def emit_health(health) -> None:
        """The part's own health, recorded beside what it captured.

        run_part produces this; the governor will consume it. Until then it goes
        to the same log, so a reader that is ticking but writing nothing is
        distinguishable from one that has stopped ticking at all.
        """
        record({"event": "part-health", **health.__dict__})

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=part_end,
            do_one_tick=tick,
            emit_health=emit_health,
            health_interval_seconds=health_interval,
        )
    finally:
        record(
            {
                "event": "capture-stopped",
                "ran_for_seconds": (time.time_ns() - started_at) / 1e9,
                "requested": stopping["requested"],
                **describe_capture(reader),
            }
        )
        reader.close()
        part_end.close()
        governor_end.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
