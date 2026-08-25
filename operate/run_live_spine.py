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
from runtime.scope_placer import ScopeLimits  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402
from runtime.switch_service import ACTION_TURN_OFF, OUTCOME_FLIPPED  # noqa: E402
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
    # First, so the earliest health reports have an inbox to land in: it consumes
    # part-health from every other part here, and a board reads the table it
    # writes. Until it was on the spine (2026-08-23) nothing consumed the
    # staleness or input loss every part had been reporting.
    "heartbeat-collector",
    # The governor's meters, complete since 2026-08-24 (RL-068: the governor
    # spine before the futures vertical). These measure the machine and the parts
    # on it and feed the planner; none of them reads the market, so they start
    # before the feed. The deciding half -- duty-cycle-planner, switching-planner,
    # off-state-verifier and gate-actuator -- starts after the feed, below.
    "hardware-scanner",
    "part-priority-reader",
    "part-appetite-meter",
    "hog-detector",
    "io-pressure-meter",
    "memory-pressure-forecaster",
    "part-restart-budgeter",
    "switch-oscillation-damper",
    "resource-reservation-ledger",
    "accelerator-scheduler",
    # The feed. These three replace operate/start_trade_capture.py entirely: the
    # catalogue picks the symbols, the planner packs them onto connections, and the
    # reader writes the tape and publishes market-data.
    "symbol-catalogue-reader",
    "stream-budget-planner",
    "venue-trade-stream-reader",
    # The quote half of the feed, added 2026-08-24. A trade is what the venue
    # printed and is what the tape keeps; a quote is what a symbol is worth right
    # now, and a symbol nobody has traded has only the second. Measured that day,
    # instrument-selector refused 525 of 9,945 intents for a price too old and
    # zero for never having seen a price -- so the refusals were symbols already
    # captured whose last trade was simply old.
    #
    # It writes no tape: this stream is priced against rather than learned from,
    # and on Bybit alone it carries 1,190 updates a second, more than every trade
    # this system records
    # (docs/proposals/an-all-market-quote-is-the-price-a-quiet-symbol-has.md).
    "venue-quote-stream-reader",
    # The governor's deciding half, acting since 2026-08-24. duty-cycle-planner
    # counts market activity per UTC hour, so it starts after the reader; the
    # switching-planner weighs all fourteen inputs into a switch-plan; and
    # gate-actuator -- the one part ever handed the switch endpoint -- carries it
    # out, started last so the first plan it acts on was built with every meter
    # already reporting. What made turning the actuator on safe is
    # part-priority.toml: the feed reader, the sampler, the recorders and the
    # collector hold the first ten ranks, resource-reservation-ledger gives each a
    # guaranteed floor, and the planner never switches a reserved part off for
    # memory, hog or io reasons -- so the plan that could have cost an hour of
    # tape is a plan the policy cannot produce. Every governor part reports its
    # standing, so a flip that happened is on the board, and one that did not is
    # too.
    "duty-cycle-planner",
    "switching-planner",
    "off-state-verifier",
    "gate-actuator",
    # Every symbol's latest price, published four times a second as one frame per
    # venue. Thirty-seven parts read this instead of every trade, which is what
    # stops fan-out scaling with trading volume -- 12,707 deliveries a second
    # measured on 2026-08-23, against 148 for the same parts on frames. Without it
    # running, every one of those parts has an input nobody produces and sits
    # there looking perfectly healthy, which is the failure this whole phase is
    # about (docs/proposals/sampled-price-levels-and-a-governor-that-acts.md).
    "price-level-sampler",
    # The same argument on a louder feed: quotes arrive five times faster than
    # trades, so publishing per update would re-create the fan-out that sampling
    # the trade feed removed. Started beside the price sampler and on the same
    # cadence -- a reader comparing a price against a quote must not be handed one
    # of them sampled more often than the other.
    "quote-level-sampler",
    # ---- phase 6a: what the forecasts are built from, 2026-08-25 -----------
    # Placed here, with the samplers, because everything downstream reads them:
    # bull-feature-builder wants the funding forecast, instrument-selector wants
    # the implied-vol surface, and luck-skill-separator cannot tell luck from
    # skill without knowing how much the market was moving.
    #
    # Volatility is the one quantity in trading that is genuinely forecastable --
    # returns are close to unpredictable and their magnitude is not, because
    # volatility clusters. That is why this is a regression and not a
    # classification: sizing needs a number, not a direction.
    # The candle feed. Without it every part below has an empty inbox, because
    # kline-window-builder filters market-data for candles and the trade reader
    # publishes trades -- so the whole prediction chain sat running and idle on
    # 2026-08-25 with nothing to build a window from.
    #
    # The closed flag is why this is its own reader rather than candles derived
    # from the trade stream: both venues push updates to the *current* candle
    # continuously, and only `k.x` on Binance and `confirm` on Bybit say which
    # update is the terminal one for that minute. A window built from partial
    # minutes is wrong in a way nothing downstream detects.
    "ccxt-venue-reader",
    "kline-window-builder",
    "implied-vol-reader",
    "funding-rate-forecaster",
    "order-flow-state-encoder",
    "flow-entropy-meter",
    "volatility-feature-builder",
    "realised-vol-regressor",
    "entropy-magnitude-forecaster",
    "liquidation-cluster-mapper",
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
    # What the labeller measured on the way, turned into the two profiles an exit
    # plan cannot be built without. They exist because the closed-trade profilers
    # cannot run until a trade has closed, and a trade cannot be opened without a
    # stop (docs/proposals/live-excursion-and-horizon-profiling.md). They read the
    # labeller's own labels rather than the feed: tracking claims is the labeller's
    # hot loop and doing it three times would be three times the cost for the same
    # numbers.
    "signal-excursion-profiler",
    "signal-horizon-profiler",
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
    # Where the exits go, decided before the entry is ever sent. Risk's own stop,
    # capped and moved clear of liquidation pools, and the target that closes the
    # trade in profit. It runs before the sizer because the sizer sizes the trade
    # against the distance to that stop -- a position sized without one is a
    # position whose risk nobody computed.
    "stop-target-placer",
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
    # Closing the position. A fill becomes a held position, the exits are chained
    # to it the instant it fills, and both rest in the paper book until a live
    # price reaches one of them (RL-071). Whichever fills, the other is withdrawn.
    "fill-reconciler",
    "cost-basis-tracker",
    "peak-excursion-tracker",
    "exit-order-chainer",
    "stop-order-manager",
    "position-close-detector",
    # Funding, booked as its own idempotent event rather than folded into a fill.
    # Added for phase 5: pnl-attributor splits a closed trade into direction,
    # timing, size, fees, slippage and funding, and without this it is a splitter
    # missing one of its terms -- which on a perpetual is not a rounding error.
    # Before the accountant, which reads the funding-settlement it produces.
    "funding-settlement-recorder",
    "usdt-pnl-accountant",
    # The record. Without it a fill happened and nothing can say what decided it,
    # and a position closed with nothing to say what it was worth.
    "trade-lifecycle-recorder",
    "position-recorder",
    # ---- phase 5: closed-trade decoding, 2026-08-25 -------------------------
    # 115 round trips had closed and nothing had scored one. Every part here
    # reads `closed-trade` and `peak-excursion`, both already produced and
    # journalled, which is what makes this the block whose whole input was
    # already live.
    #
    # They are switched on together on purpose. Most of what they need, they
    # produce for each other -- `trade-episode` alone has eight readers -- so
    # starting them one at a time would be starting each into an empty inbox.
    # The ones that still refuse name their phase: volatility-forecast is 6,
    # regime-break-alert and correlation-cluster are 12, symbol-profile is 13.
    # A part that refuses for a stated reason is the finding, not the failure.
    #
    # Every one of them refuses a trade recorded before
    # closed_trades_trustworthy_after_ns: ten of the trades already on this
    # machine carry an entry price the market never printed, and a decoder
    # trained on those learns stop placement from prices that never existed.
    "near-miss-recorder",
    "entry-quality-scorer",
    "stop-placement-auditor",
    "trade-replay-verifier",
    "pnl-attributor",
    "regime-transition-tagger",
    "trade-cluster-detector",
    "luck-skill-separator",
    "shortfall-decomposer",
    "exit-counterfactual-replayer",
    # The keystone, started after its producers and before its readers: it
    # consumes six things the parts above make, and eight parts read the
    # `trade-episode` it produces.
    "trade-episode-encoder",
    "sequence-pattern-miner",
    "exploration-pair-decoder",
    "excursion-profiler",
    "exit-quality-scorer",
    "holding-horizon-profiler",
    "loss-cause-classifier",
    "winner-pattern-miner",
    "trade-narrative-writer",
    "lesson-extractor",
    # What the system concluded, as opposed to what it did. Added 2026-08-25 with
    # pnl-attribution: until then every conclusion this system drew lived on the
    # bus and died with the process that drew it, so a board had nothing on disk
    # to show and a part started tomorrow could learn nothing from today
    # (docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md).
    # ---- phase 6b: the model, and the loop that keeps it honest ------------
    # This is a cycle by construction and is exempted as one: the model is
    # finetuned, it forecasts, forecast-scorer scores what it said against what
    # the market did, model-drift-monitor raises an alert when that accuracy
    # decays, and the alert is what triggers the next finetune. kronos-size-selector
    # closes a second loop, choosing which model size to run from the accuracy the
    # sizes themselves produced.
    #
    # Kronos-large (499.2M) is not open-source, which is why choosing a size is a
    # part at all rather than a setting.
    "kronos-size-selector",
    "kronos-finetuner",
    "kronos-forecaster",
    "forecast-distribution-gate",
    "forecast-ensembler",
    "forecast-scorer",
    "model-drift-monitor",
    # Last, after every part whose conclusions it writes down.
    "learning-recorder",
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

    # A previous run that died without its stop path -- a supervisor killed at
    # the timeout, a machine that lost power mid-stop -- leaves its parts'
    # scopes behind, and systemd refuses a second unit by the same name: every
    # placement would fail and all 57 parts would run unbounded while looking
    # started. The lock above proves no other spine is alive, so a scope named
    # for one of this spine's parts is a leftover, and stopping it kills any
    # orphaned process still inside -- which is the recovery, not a hazard: an
    # orphan holds inbox sockets the new run needs. The slice is found from this
    # process's own cgroup, the same way part-appetite-meter finds the scopes.
    from runtime.hardware_facts import read_own_cgroup_directory

    for leftover in sorted(read_own_cgroup_directory().parent.glob("*.scope")):
        part_id = leftover.name.removesuffix(".scope")
        if part_id in spine:
            subprocess.run(
                ["systemctl", "--user", "stop", leftover.name],
                capture_output=True, text=True,
            )
            record({"event": "leftover-scope-stopped", "part_id": part_id})

    # Every part in its own transient scope, with the same bounds for all --
    # limits are per-part only when a part with a genuinely larger working set
    # earns them (the settings' notes carry the measurements). The scopes are
    # what part-appetite-meter reads, so without them the metering is absent,
    # the planner refuses every plan, and the governor cannot act at all: this
    # line is what turned the governor from refusing to governing on 2026-08-24.
    # A placement that fails does not kill the part -- it runs unbounded and is
    # reported as such, which is the status quo before this existed.
    scope_limits = ScopeLimits(
        memory_max_bytes=int(settings.read_value("part_scope_memory_max_bytes")),
        cpu_weight=int(settings.read_value("part_scope_cpu_weight")),
        pids_max=int(settings.read_value("part_scope_pids_max")),
    )
    launcher = PartLauncher(
        place_in_scope=True,
        limits_for=lambda part_id: scope_limits,
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
            # True since 2026-08-24: every part is placed in its own scope with
            # the bounds the settings state, and a part a placement failed for
            # runs unbounded and is counted in the launcher's standing.
            "placed_in_scopes": True,
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

    # Parts the governor has switched off, which the restart loop below must not
    # switch back on. Without this the supervisor and gate-actuator fight: the
    # actuator stops a part, the loop sees a part not running and restarts it,
    # switch-oscillation-damper reports the flapping, and the flapping is the
    # supervision's own. The governor's off ends when the governor says on.
    governor_switched_off: set[str] = set()

    try:
        while not stopping["asked"]:
            for outcome in switch_service.serve_pending():
                record(
                    {
                        "event": "governor-switch",
                        "part_id": outcome.part_id,
                        "action": outcome.action,
                        "outcome": outcome.outcome,
                        "detail": outcome.detail,
                    }
                )
                if outcome.outcome == OUTCOME_FLIPPED:
                    if outcome.action == ACTION_TURN_OFF:
                        governor_switched_off.add(outcome.part_id)
                    else:
                        governor_switched_off.discard(outcome.part_id)
            now = time.monotonic()
            for part_id in spine:
                if part_id in governor_switched_off:
                    continue
                if launcher.is_running(part_id):
                    policy.record_healthy(part_id)
                    continue
                if not policy.may_start(part_id, now):
                    continue
                wait = policy.record_restart(part_id, now)
                # Read before the restart replaces it. Without this the log said a
                # part restarted and never how the last one ended, so a part
                # crash-looping thirteen times a minute was indistinguishable from
                # one being cycled on purpose -- and undiagnosable either way.
                last_exit_code = launcher.read_exit_code(part_id)
                try:
                    launched = launcher.start(part_id)
                    record(
                        {
                            "event": "part-restarted",
                            "last_exit_code": last_exit_code,
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
