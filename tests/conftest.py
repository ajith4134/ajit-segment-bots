"""Fixtures shared by every test in this project.

At the repository root rather than per directory because both things set up here
are needed by every test that touches the runtime or a part, and a fixture that
exists in one directory is a fixture the next directory silently does without.

pytest's own tmp_path lands under /tmp, which is tmpfs on this box. Any test that
measures durability or memory there measures nothing. So durable_tmp_path is used
instead, and it asserts what it handed back.

The BLAS thread caps are applied here, before any test module anywhere can import
numpy. Measured: with them unset, import numpy alone puts 12 kernel threads in the
process. pytest imports every conftest.py from the rootdir down before it imports
any test module, so this is the one place guaranteed to run first.

The captured-payload readers import the capture script itself rather than
re-implementing its format: two definitions of the fixture format would be two
things to keep in step, and the failure when they drifted would look like a venue
changing its messages.
"""

import importlib.util
import json
import pathlib
import shutil

import pytest

from runtime.forkserver_launcher import apply_blas_thread_caps
from runtime.storage_facts import require_durable_directory

apply_blas_thread_caps()

PROJECT = pathlib.Path(__file__).resolve().parent.parent
DURABLE_TEST_ROOT = pathlib.Path.home() / ".cache" / "ajit-segment-bots" / "tests"
CAPTURED_ROOT = PROJECT / "tests" / "captured"
CAPTURE_SCRIPT = CAPTURED_ROOT / "capture_venue_payloads.py"


@pytest.fixture
def durable_tmp_path(request) -> pathlib.Path:
    """A scratch directory on real disk, proven so before it is handed over."""
    DURABLE_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    require_durable_directory(DURABLE_TEST_ROOT)
    path = DURABLE_TEST_ROOT / request.node.name.replace("/", "_")[:120]
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _load_capture_script():
    """Import the capture script by path -- tests/captured is fixtures, not a package."""
    specification = importlib.util.spec_from_file_location("capture_venue_payloads", CAPTURE_SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def read_captured_payloads():
    """Return a reader: venue and file name -> [(received_at_ns, raw payload bytes)].

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

    def read(venue: str, name: str):
        path = CAPTURED_ROOT / venue / name
        assert path.exists(), f"{path} is missing; recapture it with capture_venue_payloads.py"
        return json.loads(path.read_text())

    return read


@pytest.fixture(scope="session")
def capture_manifest():
    """What was captured, when, from where, and how it was subset."""
    return json.loads((CAPTURED_ROOT / "capture-manifest.json").read_text())
