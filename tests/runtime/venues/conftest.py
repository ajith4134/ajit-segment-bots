"""Access to the captured venue payloads every adapter test runs against.

The reader is imported from the capture script itself rather than written again
here. Two definitions of the fixture format would be two things to keep in step,
and the failure when they drifted would look like a venue changing its messages.
"""

import importlib.util
import pathlib

import pytest

PROJECT = pathlib.Path(__file__).resolve().parents[3]
CAPTURED_ROOT = PROJECT / "tests" / "captured"
CAPTURE_SCRIPT = CAPTURED_ROOT / "capture_venue_payloads.py"


def _load_capture_script():
    """Import the capture script by path -- tests/captured is fixtures, not a package."""
    specification = importlib.util.spec_from_file_location("capture_venue_payloads", CAPTURE_SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def read_captured_payloads():
    """Return a reader: fixture file name -> [(received_at_ns, raw payload bytes)].

    Fails rather than skips when a fixture is absent. A missing capture is not a
    machine that cannot run this test; it is a test that has stopped checking
    anything, which is the thing Rule 8 says must never render as fine.
    """
    read_payload_lines = _load_capture_script().read_payload_lines

    def read(venue: str, name: str) -> list[tuple[int, bytes]]:
        path = CAPTURED_ROOT / venue / name
        assert path.exists(), (
            f"{path} is missing. Recapture it with "
            f"`.venv/bin/python tests/captured/capture_venue_payloads.py {venue} --day <YYYY-MM-DD>` "
            f"-- these tests run on what the venue actually sent (RL-063), so there is no "
            f"fallback to a fixture written by hand."
        )
        records = read_payload_lines(path)
        assert records, f"{path} is empty"
        return records

    return read


@pytest.fixture(scope="session")
def read_captured_json():
    """Return a reader for a captured REST response."""
    import json

    def read(venue: str, name: str):
        path = CAPTURED_ROOT / venue / name
        assert path.exists(), f"{path} is missing; recapture it with capture_venue_payloads.py"
        return json.loads(path.read_text())

    return read


@pytest.fixture(scope="session")
def capture_manifest():
    """What was captured, when, from where, and how it was subset."""
    import json

    return json.loads((CAPTURED_ROOT / "capture-manifest.json").read_text())
