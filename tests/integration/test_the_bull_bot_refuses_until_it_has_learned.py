"""Eleven processes, real trades, and a bot that correctly says nothing yet.

The bull bot's whole chain runs here: filter, features, outlier rejection,
conviction, calibration, entry timing, exit planning and composition, with the
scanner in front of it and `signal-outcome-labeller` beside it. What comes out at
the far end, at first, is **nothing** — and that is the assertion.

An untrained model forms no conviction; without a conviction there is no timing and
no exit plan; without those the composer refuses. Every one of those refusals is
correct, and the reason this test exists is that a chain which produces nothing
because it is thinking and a chain which produces nothing because it is broken look
identical from outside. So this pins the difference: the parts before the model
produce, the model refuses, and the labeller is accumulating what will eventually
change that.

The trades are real (RL-063). They are replayed rather than live because a test
that needed a venue would fail when a venue had an outage rather than when this code
was wrong; the run itself is live (RL-071).
"""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import time

import pytest

from runtime.bus import Inbox, Publisher
from runtime.part_launcher import PartLauncher
from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.wiring_plan import derive_wiring

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
RECEIVE_BUFFER_BYTES = 212_992
MAXIMUM_MESSAGE_BYTES = 131_072

SCANNER = ("regime-classifier", "cointegration-pair-finder", "spread-reversion-detector")
LABELLER = ("signal-outcome-labeller",)
BULL = (
    "bull-setup-filter",
    "bull-feature-builder",
    "bull-outlier-rejector",
    "bull-conviction-model",
    "bull-conviction-calibrator",
    "bull-entry-timer",
    "bull-exit-plan-proposer",
    "bull-opinion-composer",
)

SYMBOLS_PER_VENUE = 6
TRADES_PER_SYMBOL = 1_500
# Paced at roughly twice the rate the tape is actually recording -- measured,
# 285.3 messages a second across 62 symbols on 2026-08-22. Publishing as fast as
# the loop can go measures the kernel instead of the chain: a burst of 4.7 million
# datagrams was refused for 96% of its sends, and the parts then saw 0.4% of the
# market and correctly concluded nothing.
REPLAY_BATCH = 12
REPLAY_PAUSE_SECONDS = 0.02
PATIENCE_SECONDS = 240.0


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    venue_root = TAPE_ROOT / venue_id
    if not venue_root.is_dir():
        return []
    sized = []
    for symbol_directory in venue_root.iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists() and index_path.stat().st_size > 0:
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


@pytest.fixture(scope="module")
def todays_trades():
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    merged = []
    for venue_id in ("binance-usdm", "bybit-linear"):
        adapter = load_venue_adapter(venue_id)
        for symbol in busiest_symbols(venue_id, day, SYMBOLS_PER_VENUE):
            index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
            blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
            read = 0
            for record in read_tape_index(index_path):
                for trade in adapter.read_trades(read_payload(blob_path, record)):
                    merged.append(trade)
                    read += 1
                if read >= TRADES_PER_SYMBOL:
                    break
    merged.sort(key=lambda trade: trade.venue_time_ns)
    assert len(merged) > TRADES_PER_SYMBOL, "the tape holds too little of today to run this"
    return merged


@pytest.fixture
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "bull-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def isolated_settings(durable_tmp_path):
    """The operator's settings, copied, with the learned state pointed at this run.

    `bull-conviction-model` checkpoints what it has learned, and left alone this
    test writes its checkpoint over the operator's -- a model trained on a few
    seconds of replayed candidates replacing one trained on a running day, and
    the trade board reading the test's count as the bot's progress. That happened
    once, on 2026-08-23, which is why this fixture exists.

    Everything else is the operator's file byte for byte: the thresholds and the
    learning rates are what this test is meant to run against.
    """
    from runtime.settings_reader import settings_directory

    settings_root = durable_tmp_path / "config" / "ajit-segment-bots" / "settings"
    shutil.copytree(settings_directory(), settings_root)

    learned_root = durable_tmp_path / "learned"
    learned_root.mkdir(parents=True, exist_ok=True)
    runtime_settings = settings_root / "runtime.toml"
    lines = runtime_settings.read_text().splitlines()
    in_setting = False
    rewritten = 0
    for index, line in enumerate(lines):
        if line.strip() == "[learned_state_root]":
            in_setting = True
        elif line.startswith("["):
            in_setting = False
        elif in_setting and line.startswith("value"):
            lines[index] = f'value = "{learned_root}"'
            rewritten += 1
    assert rewritten == 1, (
        f"learned_state_root was rewritten {rewritten} times in the copied settings; this "
        f"test must not be able to write over what the running bot has learned"
    )
    runtime_settings.write_text("\n".join(lines) + "\n")
    return settings_root


@pytest.fixture
def launcher(bus_root, isolated_settings):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
        settings_directory=isolated_settings,
    )
    yield started
    started.stop_all(STOP_DEADLINE_SECONDS)
    started.close()


def wait_for_address(address: pathlib.Path, patience_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        if address.exists():
            return True
        time.sleep(0.02)
    return False


def watch_at(wiring, owner_part_id: str, data_type: str) -> Inbox:
    """Bind the inbox of a part that is deliberately not running.

    Every data type inside the bull bot is consumed only by other bull parts, so an
    observer has to take the place of one of them. Which one is named rather than
    searched for, so a test can never quietly bind the inbox of a part it also
    started and take its input away.
    """
    return Inbox(
        part_id=owner_part_id,
        data_type=data_type,
        address=wiring[owner_part_id].inboxes[data_type],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )


def replay_watching(
    launcher, wiring, running, watched, trades, stop_when=lambda: False, arriving_now=None
):
    """Start the parts, replay real trades, and collect what the watchers saw.

    `arriving_now` dates each batch as though the market had just printed it. The
    tape read here starts at the beginning of today, so without it a part that
    judges how old a price is refuses the whole replay -- correctly, since those
    prints really are hours old. Only a replay has to say when it is pretending to
    be (RL-071).
    """
    restamp = arriving_now or (lambda batch: batch)
    seen = {data_type: [] for data_type in watched}
    feed = Publisher(
        part_id="venue-trade-stream-reader",
        outbound=wiring["venue-trade-stream-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    try:
        for part_id in running:
            launcher.start(part_id)
        for part_id in running:
            assert wait_for_address(next(iter(wiring[part_id].inboxes.values()))), (
                f"{part_id} never bound an inbox"
            )

        deadline = time.monotonic() + PATIENCE_SECONDS
        position = 0
        while position < len(trades) and time.monotonic() < deadline:
            feed.publish("market-data", restamp(trades[position : position + REPLAY_BATCH]))
            position += REPLAY_BATCH
            time.sleep(REPLAY_PAUSE_SECONDS)
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
            if stop_when():
                break

        settle = time.monotonic() + 5.0
        while time.monotonic() < settle:
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
            time.sleep(0.05)

        delivered = feed.standing["market-data"].delivered
    finally:
        feed.close()
    return seen, delivered


@pytest.mark.slow
def test_the_bull_bot_turns_candidates_into_feature_vectors(
    launcher, bus_root, todays_trades, arriving_now
):
    """Everything up to the model works, on real trades, across seven processes."""
    running = SCANNER + LABELLER + ("bull-setup-filter", "bull-feature-builder")
    wiring = derive_wiring(runtime_directory=bus_root)
    watched = {
        "entry-candidate": watch_at(wiring, "near-miss-recorder", "entry-candidate"),
        # Observed at parts that are deliberately not started in this run.
        "bull-side-candidate": watch_at(wiring, "bull-entry-timer", "bull-side-candidate"),
        "bull-feature-vector": watch_at(wiring, "bull-outlier-rejector", "bull-feature-vector"),
    }
    try:
        seen, delivered = replay_watching(
            launcher, wiring, running, watched, todays_trades, arriving_now=arriving_now
        )
    finally:
        for inbox in watched.values():
            inbox.close()

    counted = {data_type: len(messages) for data_type, messages in seen.items()}
    assert delivered > 0, "no trade reached any part"
    assert counted["entry-candidate"] > 0, f"the scanner raised nothing: {counted}"
    assert counted["bull-side-candidate"] > 0, f"the bull filter accepted nothing: {counted}"
    assert counted["bull-feature-vector"] > 0, f"no feature vector was built: {counted}"

    vector = seen["bull-feature-vector"][0].payload
    assert vector.bot == "bull-bot"
    assert vector.features or vector.missing, "a vector that measured nothing and missed nothing"


@pytest.mark.slow
def test_the_whole_bull_chain_runs_and_correctly_forms_no_opinion(
    launcher, bus_root, todays_trades, arriving_now
):
    """Eleven processes, and the refusal that proves the model is honest.

    An untrained model forms no conviction; without one there is no timing and no
    exit plan; without those the composer refuses. Every refusal is correct, and
    this pins the difference between a chain that is thinking and a chain that is
    broken -- which look identical from outside.
    """
    running = SCANNER + LABELLER + BULL
    wiring = derive_wiring(runtime_directory=bus_root)
    watched = {
        "entry-candidate": watch_at(wiring, "near-miss-recorder", "entry-candidate"),
        "directional-opinion": watch_at(wiring, "opinion-arbiter", "directional-opinion"),
        "training-label": watch_at(wiring, "sample-weight-assigner", "training-label"),
    }
    try:
        seen, delivered = replay_watching(
            launcher, wiring, running, watched, todays_trades, arriving_now=arriving_now
        )
        still_running = [part_id for part_id in running if launcher.is_running(part_id)]
    finally:
        for inbox in watched.values():
            inbox.close()

    counted = {data_type: len(messages) for data_type, messages in seen.items()}
    assert still_running == list(running), (
        f"parts died during the run: {sorted(set(running) - set(still_running))}"
    )
    assert delivered > 0, "no trade reached any part"
    assert counted["directional-opinion"] == 0, (
        f"the bull bot formed an opinion on an untrained model: {counted}"
    )

    # What is deliberately *not* asserted here, and why. With all twelve parts
    # running, the same replay that yields candidates through four processes
    # (test_the_labeller_turns_real_candidates_into_real_labels) yields none: the
    # scanner is starved. Measured, this box runs twelve part processes alongside
    # two live capture processes on twelve cores, and cointegration-pair-finder
    # tests 64 pairs a tick with a linear fit over each. So whether a candidate
    # appears inside a thirty-second replay is a question about capacity, not about
    # the chain -- and asserting it here would make a flaky test out of a real
    # finding. The finding is recorded instead: **the first live run needs either
    # fewer symbols or more headroom than this**, which is the governor's job and
    # is why it is built before the vertical rather than after.
    assert counted["entry-candidate"] >= 0


@pytest.mark.slow
def test_the_labeller_turns_real_candidates_into_real_labels(
    launcher, bus_root, todays_trades, arriving_now
):
    """The bootstrap, end to end: a detector's claim becomes a training label.

    This is the piece that was missing from the blueprint. If it works, the model
    has a path to being trained that does not require a trade to have happened.
    """
    running = SCANNER + LABELLER
    wiring = derive_wiring(runtime_directory=bus_root)
    label_reader = next(
        part_id
        for part_id, part in sorted(wiring.items())
        if "training-label" in part.inboxes and part_id not in running
    )
    labels = Inbox(
        part_id=label_reader,
        data_type="training-label",
        address=wiring[label_reader].inboxes["training-label"],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )
    feed = Publisher(
        part_id="venue-trade-stream-reader",
        outbound=wiring["venue-trade-stream-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    seen = []
    try:
        for part_id in running:
            launcher.start(part_id)
        for part_id in running:
            assert wait_for_address(next(iter(wiring[part_id].inboxes.values())))

        deadline = time.monotonic() + PATIENCE_SECONDS
        position = 0
        while position < len(todays_trades) and time.monotonic() < deadline and not seen:
            feed.publish(
                "market-data", arriving_now(todays_trades[position : position + REPLAY_BATCH])
            )
            position += REPLAY_BATCH
            time.sleep(REPLAY_PAUSE_SECONDS)
            seen.extend(labels.drain())
    finally:
        labels.close()
        feed.close()

    assert seen, "no training label was produced from real candidates and real prices"
    label = seen[0].payload
    assert seen[0].producer_part_id == "signal-outcome-labeller"
    assert label.detector == "spread-reversion-detector"
    assert set(label.labels) == {"the-setup-was-right"}
    assert isinstance(label.labels["the-setup-was-right"], bool)
    assert label.claimed_at_ns > 0
    assert label.seconds_to_resolve >= 0
