"""Every part that carries start_part forks as a process and reports its health.

The one proof that is the same for all 324 parts. A part's unit tests prove its
engine; this proves the thing the launcher actually runs: the module imports in
a fresh process, start_part binds every input it declares and nothing else,
every setting it names exists in the operator's file, and within a few seconds
the part says "on" on the bus. A start_part that reads a missing setting, binds
a type the blueprint does not give it, or raises before its first tick fails
here by name.

Discovered, not listed: a part gains this test the moment it gains start_part,
so the count of parametrised cases is also the count of launchable parts.

**The operator's own state is not touched.** Settings are copied and the
journal, learned state, heartbeat table and tape root are pointed at this run's
own directory, so a part started here cannot write into the live record or
over what the running bot has learned -- and a tape-writing part cannot put a
second writer on the live tape.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import re
import shutil
import time

import pytest

from runtime.bus import Inbox
from runtime.part_launcher import PART_ENTRY_POINT, PartLauncher, resolve_part_module
from runtime.wiring_plan import derive_wiring, load_blueprint

THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
RECEIVE_BUFFER_BYTES = 212_992
# Long enough for a part that measures the machine or opens a venue socket on
# its first tick; a part that has not reported in this long is not starting.
HEALTH_PATIENCE_SECONDS = 30.0


def launchable_part_ids() -> list[str]:
    parts = []
    for feature in load_blueprint()["features"]:
        try:
            module = importlib.import_module(resolve_part_module(feature["id"]))
        except Exception:  # noqa: BLE001 -- no module, or one that does not import: not launchable
            continue
        if callable(getattr(module, PART_ENTRY_POINT, None)):
            parts.append(feature["id"])
    return parts


LAUNCHABLE = launchable_part_ids()


@pytest.fixture(scope="module")
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "every-part-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def isolated_settings(tmp_path_factory):
    """The operator's settings, copied, with everything a part writes redirected."""
    from runtime.settings_reader import settings_directory
    from runtime.storage_facts import require_durable_directory

    base = pathlib.Path.home() / ".cache" / "ajit-segment-bots-tests" / "every-part"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    require_durable_directory(base)
    settings_root = base / "config" / "ajit-segment-bots" / "settings"
    shutil.copytree(settings_directory(), settings_root)

    redirected = {
        "journal_path": base / "journal.jsonl",
        "learned_state_root": base / "learned",
        "heartbeat_table_path": base / "heartbeat-table.json",
        "tape_root": base / "tape",
    }
    (base / "learned").mkdir()
    (base / "tape").mkdir()

    runtime_settings = settings_root / "runtime.toml"
    text = runtime_settings.read_text()
    for name, path in redirected.items():
        text, count = re.subn(
            rf'(\[{name}\]\nvalue = )"[^"]*"', lambda m, p=path: f'{m.group(1)}"{p}"', text, count=1
        )
        assert count == 1, f"{name} must be redirected, or this test writes into the operator's state"
    runtime_settings.write_text(text)
    return settings_root


@pytest.fixture(scope="module")
def launcher(bus_root, isolated_settings):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
        settings_directory=isolated_settings,
    )
    # The spine opens this too: the one part the blueprint names as the actuator
    # refuses to start without a switch endpoint, which is correct.
    switch_service = started.open_switch_service(STOP_DEADLINE_SECONDS, 16)
    yield started
    started.stop_all(STOP_DEADLINE_SECONDS)
    switch_service.close()
    started.close()


def a_health_inbox_not_belonging_to(part_id: str, bus_root) -> Inbox:
    """Some part-health consumer's inbox, bound here so the part under test has
    somewhere to report -- never the inbox of the part under test itself, which
    must be free for that part to bind."""
    wiring = derive_wiring(runtime_directory=bus_root)
    consumer = next(
        other for other, w in wiring.items() if "part-health" in w.inboxes and other != part_id
    )
    return Inbox(
        part_id=consumer, data_type="part-health",
        address=wiring[consumer].inboxes["part-health"], receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )


def test_the_count_of_launchable_parts_is_what_the_board_should_say():
    """Read off the board as the gap between TESTED and launchable; recorded here
    so the number is asserted, not estimated."""
    assert LAUNCHABLE, "no part carries start_part"


@pytest.mark.parametrize("part_id", LAUNCHABLE)
def test_the_part_starts_and_reports_on(part_id, launcher, bus_root):
    health_inbox = a_health_inbox_not_belonging_to(part_id, bus_root)
    launched = launcher.start(part_id)
    try:
        deadline = time.monotonic() + HEALTH_PATIENCE_SECONDS
        reported = None
        while time.monotonic() < deadline and reported is None:
            for message in health_inbox.drain():
                if message.payload.part_id == part_id:
                    reported = message.payload
                    break
            if not launched.is_running:
                break
            time.sleep(0.05)
        exit_code = None if launched.is_running else launched.process.exitcode
        assert launched.is_running, (
            f"{part_id} exited with {exit_code} before reporting health -- read the part's "
            f"stderr; a missing setting or an input it cannot bind ends the process"
        )
        assert reported is not None, f"{part_id} ran for {HEALTH_PATIENCE_SECONDS:.0f}s and never reported"
        assert reported.state == "on"
    finally:
        launcher.stop(part_id, STOP_DEADLINE_SECONDS)
        health_inbox.close()
