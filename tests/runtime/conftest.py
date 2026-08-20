"""Fixtures shared by every substrate test.

pytest's own tmp_path lands under /tmp, which is tmpfs on this box. Any test that
measures durability or memory there measures nothing. So the substrate's tests use
durable_tmp_path instead, and it asserts what it handed back.
"""

import pathlib
import shutil

import pytest

from runtime.storage_facts import require_durable_directory

DURABLE_TEST_ROOT = pathlib.Path.home() / ".cache" / "ajit-segment-bots" / "tests"


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
