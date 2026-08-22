"""Run the live spine: the parts that must be on for the system to learn and trade.

    .venv/bin/python operate/run_live_spine.py --list
    .venv/bin/python operate/run_live_spine.py

**This is an operations entry point, not a part** (RL-069 is about the substrate;
this is about the operator). It stands in for the resource governor deciding what
runs, and it says so rather than pretending otherwise: `switching-planner` will
choose the on-set from measured pressure, and when it does this script is deleted.

What it owns until then:

- the launcher, and therefore every part's control socket;
- the switch endpoint, so `gate-actuator` can switch parts even though nothing is
  planning switches yet;
- restarting a part that exits, and **writing down that it did** -- a part that
  died and was quietly restarted is a hole in the record of what the system was
  doing when it made a decision;
- refusing to start when `operate/start_trade_capture.py` is already running,
  because `venue-trade-stream-reader` writes the same tape files and two writers
  on one tape make a duplicate indistinguishable from a real second print.

**Live prices, never a replay (RL-071).** The feed parts here open sockets to the
venues. The tape is written as it always was -- by the part now, instead of by the
capture script -- and nothing in this path reads it back.

The clock this starts is the model's. `signal-outcome-labeller` scores each
detector's claim against the prices that arrive next, and the conviction model
becomes measured only after `bull_minimum_training_observations` of them. Those
accrue in real time and cannot be caught up on later, which is the same reason the
tape was started the day it became possible.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

PROJECT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

# Before anything imports numpy, which the tape does.
from runtime.forkserver_launcher import apply_blas_thread_caps  # noqa: E402

apply_blas_thread_caps()

from runtime.part_launcher import PartLauncher  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402
from runtime.wiring_plan import derive_wiring  # noqa: E402

STATE_DIRECTORY = pathlib.Path.home() / ".local/share/ajit-segment-bots"
SUPERVISOR_LOG = STATE_DIRECTORY / "live-spine.jsonl"
LOCK_FILE = STATE_DIRECTORY / "live-spine.lock"

# The capture script this replaces. Both write the same tape files.
CAPTURE_SCRIPT_NAME = "operate/start_trade_capture.py"

# How often the supervisor looks at what it started. Not a decision about the
# system -- it is how long a dead part may go unnoticed, and every part reports its
# own health far more often than this.
SUPERVISION_INTERVAL_SECONDS = 2.0

# How long to wait before restarting a part that exited, and how far that grows.
# A part that dies on startup would otherwise be restarted as fast as the loop can
# fork, which turns one broken part into a busy machine.
RESTART_BACKOFF_FLOOR_SECONDS = 1.0
RESTART_BACKOFF_CEILING_SECONDS = 60.0

SWITCH_ENDPOINT_BACKLOG = 16

# The spine, in the order it is started: a part is started after the parts whose
# output it reads, so its first tick has something in its inbox rather than nothing.
# Comments say what each one is here for, because a list of part ids is a list of
# decisions and the decisions are the point.
LIVE_SPINE = (
    # The feed. These three replace operate/start_trade_capture.py entirely: the
    # catalogue picks the symbols, the planner packs them onto connections, and the
    # reader writes the tape and publishes market-data.
    "symbol-catalogue-reader",
    "stream-budget-planner",
    "venue-trade-stream-reader",
    # Noticing. The only path in the blueprint from market-data to an
    # entry-candidate without a playbook-rule, which the learning loop cannot build
    # until trades have happened.
    "regime-classifier",
    "cointegration-pair-finder",
    "spread-reversion-detector",
    # Learning. This is the part that makes everything after it possible: it turns
    # a detector's claim plus the prices that follow into a training-label, with no
    # trade required (docs/proposals/signal-outcome-labelling.md).
    "signal-outcome-labeller",
    # The bull bot. Forms no opinion at all until the conviction model is trained,
    # which is correct and is why the labeller runs beside it.
    "bull-setup-filter",
    "bull-feature-builder",
    "bull-outlier-rejector",
    "bull-conviction-model",
    "bull-conviction-calibrator",
    "bull-entry-timer",
    "bull-exit-plan-proposer",
    "bull-opinion-composer",
    # The decision. One bot means one opinion, and the arbiter's sole-opinion
    # penalty is what says so in the intent rather than the intent pretending
    # three bots agreed.
    "opinion-arbiter",
    # The settings the money comes from, read before anything is sized against
    # them. The validator is what the bounds gate refuses without: a bound checked
    # against settings nobody verified is a bound with no authority behind it.
    "main-account-settings-reader",
    "capital-allotment-reader",
    "capital-settings-validator",
    "money-mode-reader",
    # Which instrument carries the intent, and at what price and increment. The
    # selector prices the venue's own funding against the intent's horizon, which
    # is why symbol-catalogue-reader above has to be running.
    "instrument-selector",
    "tick-size-resolver",
    "exposure-limiter",
    # The size, and the two bounds it must survive: what one trade may risk and
    # what one trade may commit.
    "paper-account-keeper",
    "position-sizer",
    "trade-capital-bounds-gate",
    # The order. Stamped with the id it keeps forever, addressed by the money
    # mode, and filled by the paper book -- which is the only destination this
    # phase may reach.
    "order-idempotency-stamper",
    "order-destination-router",
    "paper-fill-simulator",
    # The record. Without it a fill happened and nothing can say what decided it.
    "trade-lifecycle-recorder",
)

# The segment this spine trades, and the only money mode it may run in. Checked
# before a part is started rather than trusted: `order-destination-router` refuses
# to address a live order and `paper-fill-simulator` refuses to simulate one, and
# this is the third check, at the one moment where refusing costs nothing. A run
# that reached a live venue is the failure this phase cannot recover from (RL-005).
TRADED_SEGMENT = "futures"
PAPER = "paper"
MONEY_MODE_SETTING = "money_mode"


def read_runtime_settings():
    return load_settings_document(settings_directory() / "runtime.toml", "runtime")


def refuse_unless_the_segment_is_on_paper() -> str:
    """The money mode this spine may run in, read from the operator's own file.

    Read here rather than assumed, and read before any part is forked. The parts
    that place and fill orders each refuse a live order on their own, and this is
    the check that costs nothing: an operator who set this segment live and then
    started this spine gets a refusal instead of fourteen processes discovering it
    one at a time.
    """
    document = load_settings_document(
        settings_directory() / "segments" / f"{TRADED_SEGMENT}.toml", TRADED_SEGMENT
    )
    mode = str(document.read_value(MONEY_MODE_SETTING))
    if mode != PAPER:
        raise SystemExit(
            f"{TRADED_SEGMENT} says {MONEY_MODE_SETTING} = {mode!r}, and this spine starts the "
            f"parts that place orders. Only {PAPER!r} may run here: RL-005 is paper first, with "
            f"full experimentation and no restriction, and live only for what paper proved."
        )
    return mode


def is_capture_script_running() -> list[str]:
    """The capture processes that would fight this one for the tape."""
    found = subprocess.run(
        ["pgrep", "-af", CAPTURE_SCRIPT_NAME], capture_output=True, text=True
    )
    return [line for line in found.stdout.splitlines() if CAPTURE_SCRIPT_NAME in line]


def record(event: dict) -> None:
    """Append one observation, flushed. A supervisor that is killed is normal."""
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    line = {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), **event}
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
        handle.flush()
    print(json.dumps(line), flush=True)


def take_the_lock():
    """One supervisor at a time. Two would start two of every part."""
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            f"another live spine already holds {LOCK_FILE}. Two supervisors would start two "
            f"of every part, and two copies of venue-trade-stream-reader would write every "
            f"message to the tape twice."
        )
    return handle


class RestartPolicy:
    """When a part that exited may be started again, and how often it has been.

    Backoff per part rather than globally: one part crashing on startup must not
    delay the restart of a different part that died for an unrelated reason.
    """

    def __init__(self, floor_seconds: float, ceiling_seconds: float) -> None:
        self._floor = floor_seconds
        self._ceiling = ceiling_seconds
        self._next_attempt_at: dict[str, float] = {}
        self._wait: dict[str, float] = {}
        self.restarts: dict[str, int] = {}

    def may_start(self, part_id: str, now: float) -> bool:
        return now >= self._next_attempt_at.get(part_id, 0.0)

    def record_restart(self, part_id: str, now: float) -> float:
        wait = min(self._wait.get(part_id, self._floor) * 2, self._ceiling)
        self._wait[part_id] = wait
        self._next_attempt_at[part_id] = now + wait
        self.restarts[part_id] = self.restarts.get(part_id, 0) + 1
        return wait

    def record_healthy(self, part_id: str) -> None:
        """A part that has stayed up starts its next backoff from the floor."""
        self._wait.pop(part_id, None)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the spine and exit")
    parser.add_argument(
        "--without-feed",
        action="store_true",
        help="run everything except the three feed parts, leaving the capture script to write the tape",
    )
    arguments = parser.parse_args(argv)

    spine = LIVE_SPINE
    if arguments.without_feed:
        spine = tuple(
            part_id
            for part_id in LIVE_SPINE
            if part_id not in ("symbol-catalogue-reader", "stream-budget-planner", "venue-trade-stream-reader")
        )

    if arguments.list:
        for part_id in spine:
            print(part_id)
        return 0

    settings = read_runtime_settings()
    money_mode = refuse_unless_the_segment_is_on_paper()
    wiring = derive_wiring()
    unknown = [part_id for part_id in spine if part_id not in wiring]
    if unknown:
        raise SystemExit(f"not in the blueprint: {unknown}")

    if not arguments.without_feed:
        fighting = is_capture_script_running()
        if fighting:
            raise SystemExit(
                "the capture script is still running:\n  "
                + "\n  ".join(fighting)
                + f"\n\nvenue-trade-stream-reader writes the same tape files, and two writers make "
                f"a duplicate record indistinguishable from a real second print. Stop the capture "
                f"first (operate/README.md), or run with --without-feed to leave it in charge of "
                f"the tape."
            )

    lock = take_the_lock()
    launcher = PartLauncher(
        place_in_scope=False,
        thread_ceiling=int(settings.read_value("fork_thread_ceiling")),
        placement_confirmation_deadline_seconds=float(
            settings.read_value("placement_confirmation_deadline")
        ),
        placement_confirmation_poll_interval_seconds=float(
            settings.read_value("placement_confirmation_poll_interval")
        ),
    )
    stop_deadline = float(settings.read_value("part_stop_deadline"))
    switch_service = launcher.open_switch_service(stop_deadline, SWITCH_ENDPOINT_BACKLOG)
    policy = RestartPolicy(RESTART_BACKOFF_FLOOR_SECONDS, RESTART_BACKOFF_CEILING_SECONDS)
    stopping = {"asked": False}

    def ask_to_stop(signal_number, _frame) -> None:
        stopping["asked"] = True
        record({"event": "stop-requested", "signal": signal_number})

    signal.signal(signal.SIGTERM, ask_to_stop)
    signal.signal(signal.SIGINT, ask_to_stop)

    record(
        {
            "event": "spine-starting",
            "parts": list(spine),
            "switch_endpoint": switch_service.address,
            "without_feed": arguments.without_feed,
            # Written into the record of the run, not only checked: what the
            # orders this spine places were addressed at is the first thing anyone
            # reading the journal afterwards needs to know.
            "segment": TRADED_SEGMENT,
            "money_mode": money_mode,
            # Said out loud: nothing is bounding these parts yet, because the
            # governor that decides what a part may have is not running.
            "placed_in_scopes": False,
        }
    )

    for part_id in spine:
        try:
            launched = launcher.start(part_id)
            record({"event": "part-started", "part_id": part_id, "pid": launched.process.pid})
        except Exception as refusal:
            record(
                {
                    "event": "part-refused",
                    "part_id": part_id,
                    "reason": f"{type(refusal).__name__}: {refusal}",
                }
            )

    try:
        while not stopping["asked"]:
            switch_service.serve_pending()
            now = time.monotonic()
            for part_id in spine:
                if launcher.is_running(part_id):
                    policy.record_healthy(part_id)
                    continue
                if not policy.may_start(part_id, now):
                    continue
                wait = policy.record_restart(part_id, now)
                try:
                    launched = launcher.start(part_id)
                    record(
                        {
                            "event": "part-restarted",
                            "part_id": part_id,
                            "pid": launched.process.pid,
                            "restarts": policy.restarts[part_id],
                            "next_backoff_seconds": wait,
                        }
                    )
                except Exception as refusal:
                    record(
                        {
                            "event": "restart-refused",
                            "part_id": part_id,
                            "reason": f"{type(refusal).__name__}: {refusal}",
                            "next_backoff_seconds": wait,
                        }
                    )
            time.sleep(SUPERVISION_INTERVAL_SECONDS)
    finally:
        record({"event": "spine-stopping", "restarts": policy.restarts})
        outcomes = launcher.stop_all(stop_deadline)
        launcher.close()
        lock.close()
        record({"event": "spine-stopped", "exit_codes": {k: v for k, v in outcomes.items()}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
