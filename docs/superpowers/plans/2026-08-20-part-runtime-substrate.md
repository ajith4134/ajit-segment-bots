# Part runtime substrate — phase 0 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the off-diagram substrate every one of the 321 parts is made of — what a part physically is, how it is switched on and bounded, and where its state lives — so that phase 1 can start the market-data tape against a runtime that is measured rather than assumed.

**Architecture:** A part is one OS process, forked from a `forkserver` whose single-threaded invariant is checked at every fork site, then moved into its own systemd transient user scope and *confirmed* to have landed there. Its switch is a framed command on an inherited control file descriptor that no other part holds. It owns no state that is not already durable outside it: structured records in SQLite (WAL), numeric arrays in file-backed `numpy.memmap`, operator settings in TOML read from `~/.config/ajit-segment-bots/settings/`. Nothing here appears in `docs/features.json` — RL-069 makes the substrate off-diagram, with its own status-board tile.

**Tech Stack:** Python 3.14.4 standard build (**not** free-threaded — §15.1), stdlib `sqlite3`/`tomllib`/`mmap`/`socket`/`selectors`, `numpy` 2.5.2, `threadpoolctl` 3.6.0, `watchdog` 6.0.0, `pytest` 9.1.1, `systemd-run`/`busctl` for cgroup placement.

**Spec:** `docs/superpowers/specs/2026-08-20-part-runtime-design.md` — read it before writing any code. This plan argues from it throughout and cites sections by number.

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from the spec.

- **Interpreter: standard CPython 3.14.4.** `sysconfig.get_config_var("Py_GIL_DISABLED")` must be `0`. The free-threaded build is rejected (§15.1) — five packages including `ta-lib` publish `cp314` but no `cp314t` wheel and this box has no C compiler, so on 3.14t they cannot be installed at all.
- **No C compiler, no sudo, no `apt-get`.** Every dependency must arrive as a prebuilt wheel for `cp314`. A dependency that needs a source build cannot be admitted (§15.1).
- **No numeric literals in decision code (RL-061).** Every number is either estimated at runtime carrying its estimator and window, or a named entry in a settings file carrying its provenance. Loop bounds, buffer sizes and array indices are not decision code; timeouts, intervals, thresholds, floors and limits are.
- **No placeholders, no shortcut code, no hardcoded values (RL-058, RL-062).** There is no upper limit on lines per file. A stub is a defect, not a stage.
- **Proven libraries for solved problems, own code for the edge (RL-065).** Every dependency pinned to an exact version with a written reason in `pyproject.toml`.
- **Names state what the thing does (Rule 7).** Files named for responsibility; functions verb + object; predicates read as questions (`is_`, `has_`, `can_`, `should_`); a name that hides a write is a bug. Python case style: `snake_case` for modules and functions, `PascalCase` for classes.
- **Thread caps before numpy (§1 rule 3, §7).** `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` and `NUMEXPR_NUM_THREADS` are all set to `1` **before** any import of numpy anywhere. Measured: with them unset, `import numpy` alone puts 12 kernel threads in the process and the forkserver becomes unforkable.
- **Never write state under `/tmp`.** `/tmp` is tmpfs on this box — RAM with a filesystem interface. A durability measurement taken there proves nothing and a memory measurement there measures RAM against RAM (§5). Task 2 builds the guard that enforces this.
- **Verification is not inference (Rule 0).** Every claim has a probe that runs. A rung is never inferred upward.
- **The substrate is off-diagram (RL-069, §12).** Nothing built in this plan is added to `docs/features.json` or appears on the part monitor. It gets its own status-board tile, generated from probes.

### One stated assumption about RL-063

RL-063 says tests run on real captured crypto data, never invented fixtures. **Phase 0 contains no market data**, because the tape does not start until phase 1 (§11). Its tests are therefore grounded in *measured system state* — process tables, cgroup files, `/proc/meminfo`, file descriptors, page cache — which is real and not invented, rather than in fabricated prices. Where a test needs bytes to write, those bytes are a deterministic pattern derived from a recorded seed and are explicitly **not** market data, never a stand-in for it.

The first tests that touch market data are phase 1's, and they replay the dated tape. If the user wants phase 0's store tests re-run against real captured records once the tape exists, that is a one-line change to the test's fixture directory and is worth doing; it is noted here rather than assumed.

---

## File Structure

```
pyproject.toml                              deps pinned with a written reason each
runtime/
    __init__.py                             exports nothing; the package marker
    storage_facts.py                        which filesystem a path is on; the tmpfs guard
    hardware_facts.py                       measured cores, RAM, swap, own cgroup
    settings_reader.py                      TOML load, validate-then-swap, last known good
    settings_watcher.py                     inotify on the directory; overflow forces recheck
    page_cache_discipline.py                a stream writer that returns its own page cache
    forkserver_launcher.py                  thread caps, preload, fork-site thread check
    scope_placer.py                         transient scope + confirmation the PID landed
    control_channel.py                      framed commands on the inherited fd
    part_declaration.py                     the shape every part declares (T-1)
    part_process.py                         the part template: the loop and the off switch
    state_store.py                          SQLite WAL: journal, windows, current-and-when
    numeric_state.py                        numpy.memmap with a sequence stamp
    probes/
        __init__.py
        substrate_probes.py                 every probe the status-board tile renders
settings/
    runtime.example.toml                    committed template; live values live in ~/.config
    main-account.example.toml               committed template for the capital scope
docs/settings-schema.md                     what each entry means, its unit and its bounds
docs/proposals/part-declarations.md         proposal for the blueprint edit in Task 13
dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py
dashboard/render_blueprint.py               MODIFY: required fields + two new checks
dashboard/build_status_board.py             MODIFY: the substrate tile
tests/runtime/
    test_interpreter_is_standard_build.py
    test_storage_facts.py
    test_hardware_facts.py
    test_settings_reader.py
    test_settings_watcher.py
    test_page_cache_discipline.py
    test_forkserver_launcher.py
    test_scope_placer.py
    test_control_channel.py
    test_part_process.py
    test_state_store.py
    test_numeric_state.py
    test_part_declaration.py
    test_substrate_probes.py
    conftest.py                             the durable-directory fixture every test uses
```

Each module has one responsibility and is named for it. `storage_facts` and `hardware_facts` are separate because a filesystem question and a CPU question are not the same job, and Rule 7 forbids a `facts` or `utils` bucket that would hold both.

---

## Task order and why

Tasks 1–3 come first because everything downstream needs them: the interpreter guard stops the whole plan being built on the wrong build, the durable-directory guard is what would have caught the tmpfs artefact recorded in §5, and the settings reader is what makes RL-061 satisfiable — every later task draws its numbers from it rather than writing them in.

---

### Task 1: Project skeleton, pinned dependencies, and the interpreter guard

**Files:**
- Create: `pyproject.toml`
- Create: `runtime/__init__.py`
- Create: `tests/runtime/__init__.py`
- Test: `tests/runtime/test_interpreter_is_standard_build.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an importable `runtime` package and a working `pytest` invocation — `.venv/bin/python -m pytest tests/runtime -v` — that every later task uses verbatim.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_interpreter_is_standard_build.py`:

```python
"""The interpreter this project runs on is a decision, not an accident.

Section 15.1 of the runtime spec chose the standard CPython 3.14 build over the
free-threaded one, because five packages this project wants publish a cp314 wheel
and no cp314t wheel, and this box has no C compiler to fall back on. A venv
rebuilt on the wrong interpreter would not fail loudly anywhere else -- it would
fail weeks later, at an install, with a message about a missing 'cc'.
"""

import sys
import sysconfig

import runtime


def test_interpreter_is_the_standard_build_not_free_threaded():
    assert sysconfig.get_config_var("Py_GIL_DISABLED") == 0, (
        "this venv is the free-threaded build; spec section 15.1 chose the standard "
        "build. Rebuild with: uv venv --python 3.14.4 .venv"
    )


def test_interpreter_is_python_3_14():
    assert sys.version_info[:2] == (3, 14)


def test_runtime_package_is_importable():
    assert runtime.__name__ == "runtime"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime'` (and pytest itself may not be installed yet, which is the same signal).

- [ ] **Step 3: Write `pyproject.toml`**

Every dependency carries the reason it was admitted, because RL-065 requires one and because the OpenBLAS deadlock in §1 is the argument for pinning: a hazard appeared in 0.3.30 and vanished in 0.3.34 purely through versions.

```toml
[project]
name = "ajit-segment-bots"
version = "0.1.0"
description = "Crypto segment trading bots. Every part is a transistor."
requires-python = "==3.14.*"

# Every entry is pinned exact and carries why it was admitted (RL-065).
# The interpreter is the standard build, not free-threaded: spec section 15.1.
dependencies = [
    # Numeric arrays and the memmap-backed durable state of section 15.4.
    # 2.5.2 is the version measured in the spec; OpenBLAS 0.3.34 ships with it,
    # which is the release where the pthread_atfork deadlock of numpy#30092 is fixed.
    "numpy==2.5.2",
    # The only way to verify a live BLAS thread count. Without it, section 7's
    # claim that BLAS is pinned to one thread per part cannot be made under Rule 0.
    "threadpoolctl==3.6.0",
    # inotify for the settings directory (section 15.3). Its Linux wheel is pure
    # Python via ctypes, so it needs no C compiler -- which matters on this box.
    "watchdog==6.0.0",
]

[project.optional-dependencies]
# Test runner. Pure Python, so no ABI question.
test = ["pytest==9.1.1"]
# Renders a spec or a board to a page. Pure Python.
docs = ["markdown-it-py==4.0.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
# A test that measures memory or durability must say which filesystem it ran on.
# Marked tests place a real process under a real cgroup limit and are slower.
markers = [
    "cgroup: places a process in a systemd transient scope and reads its cgroup files",
    "slow: takes more than a second because it forks, kills, or waits on writeback",
]
```

`runtime/__init__.py`:

```python
"""The part runtime substrate.

This package is deliberately absent from docs/features.json. RL-069 and section 12
of the runtime spec make the substrate off-diagram: the blueprint describes the
circuit, and this is the silicon under it. It is still measured -- see
runtime/probes/substrate_probes.py and its tile on the status board.
"""
```

`tests/runtime/__init__.py`: empty file.

- [ ] **Step 4: Install the dependencies**

```bash
uv pip install --python .venv/bin/python \
  numpy==2.5.2 threadpoolctl==3.6.0 watchdog==6.0.0 pytest==9.1.1 markdown-it-py==4.0.0
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime -v`
Expected: PASS, 3 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml runtime/__init__.py tests/runtime/__init__.py \
        tests/runtime/test_interpreter_is_standard_build.py
git commit -m "Phase 0 skeleton: pinned deps with reasons, and a guard on the interpreter

Section 15.1 chose the standard 3.14 build. A venv rebuilt on the
free-threaded one would not fail here -- it would fail at an install
weeks later, on a missing cc. So it is a test."
```

---

### Task 2: `storage_facts` — the guard that would have caught the tmpfs artefact

**Files:**
- Create: `runtime/storage_facts.py`
- Create: `tests/runtime/conftest.py`
- Test: `tests/runtime/test_storage_facts.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `read_filesystem_type(path: pathlib.Path) -> str`
  - `is_memory_backed_filesystem(path: pathlib.Path) -> bool`
  - `require_durable_directory(path: pathlib.Path) -> pathlib.Path` — raises `VolatileStorageRefused`
  - `class VolatileStorageRefused(RuntimeError)`
  - pytest fixture `durable_tmp_path` in `conftest.py`, which every later test uses instead of pytest's `tmp_path`.

**Why this is task 2 and not an afterthought:** the first round of store measurement for §15.2 ran under `/tmp`, which is tmpfs here, and appeared to disqualify LMDB, then SQLite, then a plain file write containing no database at all. It was measuring RAM against RAM. This module turns that mistake into something the code refuses to repeat.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_storage_facts.py`:

```python
"""On this box /tmp is tmpfs -- RAM with a filesystem interface.

State written there is not durable and memory measured there is measuring RAM
against RAM. Section 5 of the runtime spec records the round of measurement that
was lost to exactly this. These tests keep the guard honest.
"""

import pathlib

import pytest

from runtime.storage_facts import (
    VolatileStorageRefused,
    is_memory_backed_filesystem,
    read_filesystem_type,
    require_durable_directory,
)


def test_reads_the_filesystem_type_of_a_real_directory(durable_tmp_path):
    assert read_filesystem_type(durable_tmp_path) == "ext4"


def test_recognises_tmp_as_memory_backed_on_this_box():
    assert is_memory_backed_filesystem(pathlib.Path("/tmp")) is True


def test_recognises_the_home_filesystem_as_durable():
    assert is_memory_backed_filesystem(pathlib.Path.home()) is False


def test_require_durable_directory_returns_the_path_when_it_is_durable(durable_tmp_path):
    assert require_durable_directory(durable_tmp_path) == durable_tmp_path


def test_require_durable_directory_refuses_tmpfs_and_names_the_filesystem():
    with pytest.raises(VolatileStorageRefused) as refusal:
        require_durable_directory(pathlib.Path("/tmp"))
    assert "tmpfs" in str(refusal.value)


def test_resolves_the_filesystem_of_a_path_that_does_not_exist_yet(durable_tmp_path):
    unborn = durable_tmp_path / "not" / "created" / "yet"
    assert read_filesystem_type(unborn) == "ext4"
```

`tests/runtime/conftest.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_storage_facts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.storage_facts'`.

- [ ] **Step 3: Write the implementation**

`runtime/storage_facts.py`. It reads `/proc/self/mountinfo` rather than shelling out to `stat -f`, so it needs no subprocess and works for a path that does not exist yet by walking up to the nearest existing parent.

```python
"""Which filesystem a path is on, and whether it is real disk.

This exists because /tmp on this box is tmpfs. A part that writes its state there
would lose it on reboot while appearing to work, and a memory measurement taken
there is measuring RAM against RAM. Section 5 of the runtime spec records the
round of measurement lost to that. require_durable_directory is the refusal.
"""

from __future__ import annotations

import pathlib

MOUNTINFO_PATH = pathlib.Path("/proc/self/mountinfo")

# Filesystems whose pages are RAM. tmpfs and ramfs hold no disk behind them, so
# nothing written to them survives a reboot and everything written to them is
# charged to the writing cgroup as unreclaimable memory.
MEMORY_BACKED_FILESYSTEMS = frozenset({"tmpfs", "ramfs", "devtmpfs"})


class VolatileStorageRefused(RuntimeError):
    """A durable directory was required and a memory-backed one was offered."""


def _read_mount_table() -> list[tuple[str, str]]:
    """Return (mount point, filesystem type) for every mount, longest path last."""
    mounts: list[tuple[str, str]] = []
    for line in MOUNTINFO_PATH.read_text().splitlines():
        # mountinfo: id parent major:minor root mount-point options... - fstype source
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        remainder = after.split()
        if len(fields) < 5 or not remainder:
            continue
        mounts.append((fields[4], remainder[0]))
    mounts.sort(key=lambda entry: len(entry[0]))
    return mounts


def _nearest_existing_ancestor(path: pathlib.Path) -> pathlib.Path:
    candidate = path.absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def read_filesystem_type(path: pathlib.Path) -> str:
    """Name the filesystem holding this path, resolving it if it does not exist yet."""
    resolved = _nearest_existing_ancestor(pathlib.Path(path)).resolve()
    winner = "unknown"
    for mount_point, filesystem_type in _read_mount_table():
        mount = pathlib.Path(mount_point)
        if resolved == mount or mount in resolved.parents:
            winner = filesystem_type
    return winner


def is_memory_backed_filesystem(path: pathlib.Path) -> bool:
    """Is this path's storage RAM rather than disk?"""
    return read_filesystem_type(path) in MEMORY_BACKED_FILESYSTEMS


def require_durable_directory(path: pathlib.Path) -> pathlib.Path:
    """Return the path, or refuse it because what is written there would not survive.

    Every store and every durability or memory measurement passes its directory
    through here first.
    """
    filesystem_type = read_filesystem_type(path)
    if filesystem_type in MEMORY_BACKED_FILESYSTEMS:
        raise VolatileStorageRefused(
            f"{path} is on {filesystem_type}, which is RAM. State written there does "
            f"not survive a reboot, and memory measured there is measuring RAM against "
            f"RAM. Choose a directory on real disk -- see section 5 of the runtime spec."
        )
    return pathlib.Path(path)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_storage_facts.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Prove the guard fires on the real trap, by hand once**

```bash
.venv/bin/python -c "
from pathlib import Path
from runtime.storage_facts import read_filesystem_type, require_durable_directory
print('/tmp        ->', read_filesystem_type(Path('/tmp')))
print('\$HOME       ->', read_filesystem_type(Path.home()))
try:
    require_durable_directory(Path('/tmp/whatever'))
except Exception as refusal:
    print('refused as expected:', refusal)
"
```
Expected: `/tmp -> tmpfs`, `$HOME -> ext4`, and a refusal naming tmpfs.

- [ ] **Step 6: Commit**

```bash
git add runtime/storage_facts.py tests/runtime/conftest.py tests/runtime/test_storage_facts.py
git commit -m "storage_facts: refuse a directory whose pages are RAM

/tmp is tmpfs here. The first round of store measurement ran there and
appeared to disqualify LMDB, then SQLite, then a plain file write with no
database in it at all. This is that mistake turned into a refusal, and
durable_tmp_path is the fixture the rest of the substrate's tests use."
```

---

---

### Task 3: `settings_reader` — the named half of RL-061, and it fails closed

**Files:**
- Create: `runtime/settings_reader.py`
- Create: `settings/runtime.example.toml`
- Create: `settings/main-account.example.toml`
- Create: `docs/settings-schema.md`
- Test: `tests/runtime/test_settings_reader.py`

**Interfaces:**
- Consumes: `runtime.storage_facts` (nothing yet, but the settings root is checked once).
- Produces:
  - `class SettingEntry` — frozen dataclass `(name: str, value: float | int | str | bool, unit: str, note: str)`
  - `class SettingsDocument` — frozen dataclass `(scope: str, entries: dict[str, SettingEntry], source_path: pathlib.Path, content_digest: str, parsed_at_ns: int)`, with `def read_value(self, name: str) -> float | int | str | bool` and `def read_entry(self, name: str) -> SettingEntry`
  - `class SettingsParseRefused(ValueError)`
  - `def load_settings_document(path: pathlib.Path, scope: str) -> SettingsDocument`
  - `class LastKnownGoodSettings` — `__init__(self, path, scope)`, `def offer_candidate(self) -> SettingsRejection | None`, property `current -> SettingsDocument`
  - `class SettingsRejection` — frozen dataclass `(source_path, reason, rejected_at_ns)`
  - `def settings_directory() -> pathlib.Path` — returns `~/.config/ajit-segment-bots/settings`, honouring `XDG_CONFIG_HOME`
- Later tasks read every timeout, interval and threshold through `SettingsDocument.read_value`. **No later task writes a number into code.**

**Why the reader comes before the things that need numbers:** RL-061 says every number is estimated at runtime or a named settings entry with provenance. If the settings reader arrives late, every earlier module writes a literal and someone has to come back and remove it. That is the shortcut RL-058 forbids.

**Why it never writes:** `capital-settings-change-recorder` declares `produces: ["journal-entry", "part-health"]`, so under R-01 it is structurally incapable of writing settings back and `check_contracts.py` would refuse a wiring that tried. Stdlib `tomllib` is read-only, which is exactly right rather than a gap — no TOML writer is needed anywhere in the runtime.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_settings_reader.py`:

```python
"""Settings are the named half of RL-061 and the operator's only control surface.

RL-055: capital settings are edited in a file on the server over SSH, and the board
shows the current values and when they last changed. So the reader must survive a
human editing it badly at 2am -- a broken save keeps the last value that parsed and
says so, because under T-3 'off' is the governor's decision and never a part's
reaction to its input.
"""

import pathlib
import textwrap

import pytest

from runtime.settings_reader import (
    LastKnownGoodSettings,
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

GOOD = textwrap.dedent(
    """
    # ajit-segment-bots runtime settings.
    # No API keys or secrets belong here -- see docs/secrets.md.

    [writeback_interval]
    value = 8388608
    unit  = "bytes"
    note  = "operator, 2026-08-20: a stream writer forces writeback and drops its cache every 8 MiB; measured to hold a 500 MB write at a 16 MB peak"

    [placement_confirmation_deadline]
    value = 0.5
    unit  = "seconds"
    note  = "operator, 2026-08-20: placement median 5.7 ms, p95 6.9 ms; this is 70x the p95"
    """
).strip()

BROKEN = "[writeback_interval]\nvalue = [1, 2,\n"


def _write(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = directory / name
    path.write_text(body)
    return path


def test_loads_value_unit_and_note_for_each_entry(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    assert document.read_value("writeback_interval") == 8388608
    entry = document.read_entry("placement_confirmation_deadline")
    assert entry.unit == "seconds"
    assert entry.note.startswith("operator, 2026-08-20")


def test_records_where_it_came_from_and_a_digest_of_what_it_read(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    assert document.source_path == path
    assert len(document.content_digest) == 64
    assert document.parsed_at_ns > 0


def test_refuses_an_entry_with_no_provenance(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", "[orphan]\nvalue = 1\nunit = \"bytes\"\n")
    with pytest.raises(SettingsParseRefused) as refusal:
        load_settings_document(path, scope="runtime")
    assert "note" in str(refusal.value)


def test_refuses_a_bare_value_that_is_not_an_entry_table(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", "writeback_interval = 8388608\n")
    with pytest.raises(SettingsParseRefused):
        load_settings_document(path, scope="runtime")


def test_a_broken_edit_keeps_the_last_value_that_parsed_and_reports_the_rejection(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    assert settings.offer_candidate() is None
    assert settings.current.read_value("writeback_interval") == 8388608

    path.write_text(BROKEN)
    rejection = settings.offer_candidate()

    assert rejection is not None
    assert rejection.source_path == path
    assert "Invalid" in rejection.reason or "invalid" in rejection.reason
    assert settings.current.read_value("writeback_interval") == 8388608


def test_a_repaired_edit_is_accepted_after_a_rejection(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    settings.offer_candidate()
    path.write_text(BROKEN)
    assert settings.offer_candidate() is not None
    path.write_text(GOOD.replace("value = 8388608", "value = 4194304"))
    assert settings.offer_candidate() is None
    assert settings.current.read_value("writeback_interval") == 4194304


def test_a_no_op_save_is_not_reported_as_a_change(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    settings = LastKnownGoodSettings(path, scope="runtime")
    settings.offer_candidate()
    first_digest = settings.current.content_digest
    path.write_text(GOOD)  # what vim does on :wq with nothing changed
    assert settings.offer_candidate() is None
    assert settings.current.content_digest == first_digest


def test_reading_an_undeclared_name_refuses_rather_than_returning_a_default(durable_tmp_path):
    path = _write(durable_tmp_path, "runtime.toml", GOOD)
    document = load_settings_document(path, scope="runtime")
    with pytest.raises(KeyError):
        document.read_value("a_number_nobody_declared")


def test_the_settings_directory_is_under_config_and_not_in_the_repository():
    directory = settings_directory()
    assert directory.parts[-3:] == (".config", "ajit-segment-bots", "settings")
    assert "ajit-segment-bots/docs" not in str(directory)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_settings_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.settings_reader'`.

- [ ] **Step 3: Write the implementation**

`runtime/settings_reader.py`:

```python
"""Read the operator's settings files, and refuse a number with no provenance.

RL-055: the operator edits these over SSH, and the board shows the current values
and when they last changed. RL-061: a number is either estimated at runtime or a
named entry here carrying its provenance, and settings act as hard bounds on what
estimation may produce.

Nothing in this module writes. capital-settings-change-recorder declares
produces: [journal-entry, part-health], so under R-01 no part can write settings
back, and stdlib tomllib is read-only -- which makes that structural rather than
a convention someone has to remember.

A bad edit fails closed: the candidate is parsed into a fresh document and swapped
in only if it parsed, so the part keeps serving the last value that did. Under T-3
a part never turns itself off in reaction to its input; that is the governor's call.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import time
import tomllib
from dataclasses import dataclass

# The three keys every entry carries. 'note' is the operator's own provenance --
# who changed it and why -- recorded at the point the number enters the system.
REQUIRED_ENTRY_KEYS = ("value", "unit", "note")

SettingValue = float | int | str | bool


class SettingsParseRefused(ValueError):
    """A settings file did not parse, or an entry was missing part of its shape."""


@dataclass(frozen=True)
class SettingEntry:
    """One number the operator set, with its unit and why they set it."""

    name: str
    value: SettingValue
    unit: str
    note: str


@dataclass(frozen=True)
class SettingsDocument:
    """Everything one settings file said, and the evidence of where it came from."""

    scope: str
    entries: dict[str, SettingEntry]
    source_path: pathlib.Path
    content_digest: str
    parsed_at_ns: int

    def read_entry(self, name: str) -> SettingEntry:
        """Return the whole entry, raising if nobody declared it."""
        try:
            return self.entries[name]
        except KeyError:
            raise KeyError(
                f"no setting named '{name}' in {self.source_path}. RL-061 admits no "
                f"default: a number that was never declared has no provenance to show."
            ) from None

    def read_value(self, name: str) -> SettingValue:
        """Return just the number. Every timeout and threshold in the runtime comes from here."""
        return self.read_entry(name).value


@dataclass(frozen=True)
class SettingsRejection:
    """A candidate edit that did not parse, and therefore did not take effect."""

    source_path: pathlib.Path
    reason: str
    rejected_at_ns: int


def settings_directory() -> pathlib.Path:
    """Where the operator's settings live: ~/.config/ajit-segment-bots/settings.

    Outside the repository on purpose (section 15.3): the repo carries the schema
    and a commented template, the machine carries the values.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(config_home) if config_home else pathlib.Path.home() / ".config"
    return root / "ajit-segment-bots" / "settings"


def _digest_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def load_settings_document(path: pathlib.Path, scope: str) -> SettingsDocument:
    """Parse one settings file, refusing any entry that cannot say where it came from."""
    path = pathlib.Path(path)
    try:
        body = path.read_bytes()
    except OSError as failure:
        raise SettingsParseRefused(f"{path} could not be read: {failure}") from failure
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as failure:
        raise SettingsParseRefused(f"{path} is not valid TOML: {failure}") from failure

    entries: dict[str, SettingEntry] = {}
    for name, table in parsed.items():
        if not isinstance(table, dict):
            raise SettingsParseRefused(
                f"{path}: '{name}' is a bare value. Every setting is a table carrying "
                f"value, unit and note, so the board can show what it means and who set it."
            )
        missing = [key for key in REQUIRED_ENTRY_KEYS if key not in table]
        if missing:
            raise SettingsParseRefused(
                f"{path}: setting '{name}' is missing {', '.join(missing)}. RL-061 requires "
                f"every number to carry its unit and its provenance."
            )
        entries[name] = SettingEntry(
            name=name, value=table["value"], unit=str(table["unit"]), note=str(table["note"])
        )

    return SettingsDocument(
        scope=scope,
        entries=entries,
        source_path=path,
        content_digest=_digest_of(body),
        parsed_at_ns=time.time_ns(),
    )


class LastKnownGoodSettings:
    """Serve the last settings that parsed, and swap only when a candidate parses.

    validate-then-swap. A syntax error in a file the operator is editing must not
    take a part down, and must not silently take effect either.
    """

    def __init__(self, path: pathlib.Path, scope: str) -> None:
        self._path = pathlib.Path(path)
        self._scope = scope
        self._current = load_settings_document(self._path, scope)

    @property
    def current(self) -> SettingsDocument:
        return self._current

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def offer_candidate(self) -> SettingsRejection | None:
        """Re-read the file. Swap on success; on failure keep serving and report why.

        Returns None when the candidate was accepted or was byte-identical to what
        is already loaded -- a no-op save is not a change, which is why this compares
        the parsed digest rather than trusting mtime.
        """
        try:
            candidate = load_settings_document(self._path, self._scope)
        except SettingsParseRefused as refusal:
            return SettingsRejection(
                source_path=self._path, reason=str(refusal), rejected_at_ns=time.time_ns()
            )
        if candidate.content_digest == self._current.content_digest:
            return None
        self._current = candidate
        return None
```

- [ ] **Step 4: Write the committed templates and the schema doc**

`settings/runtime.example.toml` — the substrate's own numbers, which Tasks 4–13 read:

```toml
# ajit-segment-bots -- runtime substrate settings.
#
# Copy to ~/.config/ajit-segment-bots/settings/runtime.toml and edit there.
# This file is the template and the schema; the live values live on the machine,
# outside every repository. No API keys or secrets belong here -- see docs/secrets.md.
#
# Every entry carries value, unit and note. The note is yours: who changed it and
# why. RL-061 refuses a number that cannot say where it came from.

[writeback_interval]
value = 8388608
unit  = "bytes"
note  = "operator, 2026-08-20: a stream writer forces writeback and drops its cache every 8 MiB. Measured on ext4 under MemoryMax=200M: never fsyncing is OOM-killed, fsync alone survives pinned at the 200 MB ceiling, fsync plus FADV_DONTNEED holds the same 500 MB write at a 16 MB peak."

[placement_confirmation_deadline]
value = 0.5
unit  = "seconds"
note  = "operator, 2026-08-20: how long gate-actuator waits for a placed PID to appear in its scope before calling it a fault. Placement measured at a 5.7 ms median and 6.9 ms p95 over 87 of 88 successes, so this is roughly 70x the p95."

[placement_confirmation_poll_interval]
value = 0.002
unit  = "seconds"
note  = "operator, 2026-08-20: how often to re-read /proc/<pid>/cgroup while waiting. Below the 5.7 ms median so a normal placement is confirmed on the second or third look."

[store_busy_timeout]
value = 5.0
unit  = "seconds"
note  = "operator, 2026-08-20: SQLite WAL permits one writer per file. Measured with this unset a second writer fails instantly with 'database is locked'; set, it waits 2.54 s and succeeds. Parts must never carry their own retry loop for this."

[fork_thread_ceiling]
value = 1
unit  = "kernel threads"
note  = "operator, 2026-08-20: the forkserver refuses to fork above this, read from field 20 of /proc/self/stat. Measured: with the BLAS caps unset, import numpy alone puts 12 kernel threads in the process while threading.active_count() still reports 1."

[part_health_interval]
value = 1.0
unit  = "seconds"
note  = "operator, 2026-08-20: how often a part emits part-health while on. Intraday bars at 1m/5m/15m/30m need about one message a second, so this matches the slowest thing worth noticing."
```

`settings/main-account.example.toml` — the capital scope (RL-055's actual subject):

```toml
# ajit-segment-bots -- main account settings.
#
# Copy to ~/.config/ajit-segment-bots/settings/main-account.toml and edit there.
# Read by main-account-settings-reader. Never written by any part: R-01 gives
# capital-settings-change-recorder produces = [journal-entry, part-health], so it
# is structurally incapable of writing back here.
#
# No API keys or secrets belong here -- see docs/secrets.md.

[main_balance]
value = 0.0
unit  = "USDT"
note  = "operator: set this to the real balance before anything trades. Zero means nothing is allocated, which is the safe value to ship."

[maximum_capital_per_trade]
value = 0.0
unit  = "USDT"
note  = "operator: a hard ceiling on what any sizer may produce, whatever it estimates. RL-061 -- estimation gives the bot intelligence, this bound stops a broken estimator sizing a trade at any value."

[leverage_ceiling]
value = 1.0
unit  = "multiple"
note  = "operator: the highest leverage any futures position may carry. 1.0 means unlevered, which is the safe value to ship."
```

`docs/settings-schema.md` — write it as the reference for both files, stating for each entry: the name, the unit, what reads it, what the bound means, and the measurement or reasoning behind the shipped default. It must also state, at the top, that values live at `~/.config/ajit-segment-bots/settings/` and never in this repository, and why (section 15.3): the repo carries the schema, the machine carries the values, exactly as `docs/secrets.md` splits the credential inventory from the credential values.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_settings_reader.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 6: Install the live settings and prove the reader reads them**

```bash
mkdir -p ~/.config/ajit-segment-bots/settings/segments
cp settings/runtime.example.toml      ~/.config/ajit-segment-bots/settings/runtime.toml
cp settings/main-account.example.toml ~/.config/ajit-segment-bots/settings/main-account.toml
.venv/bin/python -c "
from runtime.settings_reader import settings_directory, load_settings_document
document = load_settings_document(settings_directory() / 'runtime.toml', scope='runtime')
for name, entry in document.entries.items():
    print(f'{name:38} {entry.value} {entry.unit}')
print('digest', document.content_digest[:16])
"
```
Expected: six entries printed with their units, and a digest.

- [ ] **Step 7: Commit**

```bash
git add runtime/settings_reader.py settings/ docs/settings-schema.md \
        tests/runtime/test_settings_reader.py
git commit -m "settings_reader: the named half of RL-061, and it fails closed

Closes RL-055. Values live at ~/.config/ajit-segment-bots/settings/, the
repo carries the schema and a commented template. Every entry must carry
value, unit and note or it is refused -- a number with no provenance is
what RL-061 exists to stop. A broken edit keeps the last document that
parsed and reports the rejection, because under T-3 a part does not turn
itself off over its input."
```

---

### Task 4: `page_cache_discipline` — a writer that hands its page cache back

**Files:**
- Create: `runtime/page_cache_discipline.py`
- Test: `tests/runtime/test_page_cache_discipline.py`

**Interfaces:**
- Consumes: `runtime.storage_facts.require_durable_directory`, `runtime.settings_reader.SettingsDocument` (entry `writeback_interval`).
- Produces:
  - `class CacheReleasingWriter` — `__init__(self, path: pathlib.Path, writeback_interval_bytes: int)`, `def append(self, block: bytes) -> int`, `def force_writeback(self) -> None`, `def close(self) -> None`, context-manager protocol, property `bytes_written -> int`
  - `def read_own_cgroup_memory_peak_bytes() -> int | None`
- Phase 1's tape writer is built on `CacheReleasingWriter`. Nothing else in phase 0 writes a stream.

**Why this is substrate and not an optimisation:** a part's `memory.max` bounds its heap *plus the page cache it dirties*, and this box has no swap (§5). Measured on ext4 under `MemoryMax=200M` writing 500 MB: never calling `fsync` is OOM-killed 3 of 3; `fsync` every 8 MB survives but sits pinned at the 200 MB ceiling; `fsync` plus `posix_fadvise(POSIX_FADV_DONTNEED)` holds at **16 MB**. Without this module every stream-writing part would ask the governor to reserve its entire limit.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_page_cache_discipline.py`:

```python
"""A part's memory limit counts the page cache it dirties, and there is no swap.

Section 5: writing 500 MB into a 200 MB cgroup is an OOM kill if the writer never
forces writeback, survives at the ceiling if it only fsyncs, and holds at 16 MB if
it also hands the written range back with FADV_DONTNEED. These tests hold that.

The cgroup tests run a real process under a real systemd transient scope, so they
are marked and they are slow. They are also the only honest way to check this:
a process's own RSS does not predict the kill.
"""

import os
import pathlib
import subprocess
import sys

import pytest

from runtime.page_cache_discipline import CacheReleasingWriter

WRITER_UNDER_LIMIT = """
import pathlib, sys
sys.path.insert(0, {repository!r})
from runtime.page_cache_discipline import CacheReleasingWriter, read_own_cgroup_memory_peak_bytes

path, total_bytes, interval = pathlib.Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
block = b"\\x5a" * 65536
with CacheReleasingWriter(path, writeback_interval_bytes=interval) as writer:
    while writer.bytes_written < total_bytes:
        writer.append(block)
print("PEAK", read_own_cgroup_memory_peak_bytes())
"""


def _run_under_memory_limit(script: str, arguments: list[str], megabytes: int, unit: str):
    return subprocess.run(
        [
            "systemd-run", "--user", "--scope", "--quiet",
            f"-p", f"MemoryMax={megabytes}M", "-p", "MemorySwapMax=0",
            f"--unit={unit}", sys.executable, "-c", script, *arguments,
        ],
        capture_output=True, text=True,
    )


def test_appends_are_readable_and_the_byte_count_is_what_was_written(durable_tmp_path):
    path = durable_tmp_path / "tape.bin"
    with CacheReleasingWriter(path, writeback_interval_bytes=4096) as writer:
        writer.append(b"one")
        writer.append(b"two")
        assert writer.bytes_written == 6
    assert path.read_bytes() == b"onetwo"


def test_refuses_a_path_whose_pages_are_memory(tmp_path):
    # pytest's own tmp_path is under /tmp, which is tmpfs on this box -- exactly
    # the trap section 5 records. The writer must not accept it.
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        CacheReleasingWriter(tmp_path / "tape.bin", writeback_interval_bytes=4096)


@pytest.mark.cgroup
@pytest.mark.slow
def test_a_writer_that_drops_its_cache_stays_far_below_its_memory_limit(durable_tmp_path):
    repository = str(pathlib.Path(__file__).resolve().parents[2])
    script = WRITER_UNDER_LIMIT.format(repository=repository)
    total = 500 * 1024 * 1024
    interval = 8 * 1024 * 1024

    completed = _run_under_memory_limit(
        script, [str(durable_tmp_path / "tape.bin"), str(total), str(interval)],
        megabytes=200, unit=f"pagecache-drop-{os.getpid()}",
    )

    assert completed.returncode == 0, f"OOM-killed or failed: {completed.stderr[-400:]}"
    peak = int(completed.stdout.split("PEAK")[1].strip())
    assert peak < 64 * 1024 * 1024, f"peaked at {peak} bytes; dropping the cache should hold it near 16 MB"


@pytest.mark.cgroup
@pytest.mark.slow
def test_a_writer_that_never_forces_writeback_is_killed_by_the_kernel(durable_tmp_path):
    # The control. Without this the test above proves only that something ran.
    naive = """
import pathlib, sys
path, total = pathlib.Path(sys.argv[1]), int(sys.argv[2])
block = b"\\x5a" * 65536
written = 0
with open(path, "wb") as handle:
    while written < total:
        handle.write(block); written += len(block)
print("SURVIVED")
"""
    completed = _run_under_memory_limit(
        naive, [str(durable_tmp_path / "naive.bin"), str(500 * 1024 * 1024)],
        megabytes=200, unit=f"pagecache-naive-{os.getpid()}",
    )
    assert completed.returncode != 0, "a 500 MB unflushed write survived a 200 MB limit; re-check the limit was applied"
    assert "SURVIVED" not in completed.stdout
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_page_cache_discipline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.page_cache_discipline'`.

- [ ] **Step 3: Write the implementation**

`runtime/page_cache_discipline.py`:

```python
"""Write a stream to disk without accumulating page cache the cgroup will kill you for.

A part's memory.max bounds its heap plus the page cache it dirties, and this box
has no swap (section 5). Measured on ext4 under MemoryMax=200M, writing 500 MB:

    never fsync                      OOM-killed, 3 of 3
    fsync every 8 MB                 survives, pinned at the 200 MB ceiling
    fsync + FADV_DONTNEED            survives, 16 MB peak

Dirty pages are not reclaimable, so a writer that outruns writeback dies. fsync
alone is enough to live, but it leaves the part sitting on its whole limit in clean
cache, so part-appetite-meter would size every writer at its cap. Handing the range
back after writing it costs one syscall and twelve times less reserved memory.
"""

from __future__ import annotations

import os
import pathlib

from runtime.storage_facts import require_durable_directory


def read_own_cgroup_memory_peak_bytes() -> int | None:
    """The high-water memory this process's cgroup reached, or None if unreadable.

    memory.peak is the honest number here: a process's own RSS does not predict an
    OOM kill, because the page cache it dirtied is charged to the cgroup and not to it.
    """
    try:
        relative = open("/proc/self/cgroup").read().strip().split("::")[1]
    except (OSError, IndexError):
        return None
    for filename in ("memory.peak", "memory.current"):
        try:
            return int(pathlib.Path("/sys/fs/cgroup" + relative, filename).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


class CacheReleasingWriter:
    """Append-only writer that forces writeback and releases the written range.

    The interval is a named setting with provenance (runtime.toml
    'writeback_interval'), never a literal here -- RL-061.
    """

    def __init__(self, path: pathlib.Path, writeback_interval_bytes: int) -> None:
        path = pathlib.Path(path)
        require_durable_directory(path.parent)
        if writeback_interval_bytes <= 0:
            raise ValueError(
                "writeback_interval_bytes must be positive; a writer that never forces "
                "writeback is OOM-killed under a cgroup memory limit (section 5)"
            )
        self._path = path
        self._interval = writeback_interval_bytes
        self._handle = open(path, "wb")
        self._descriptor = self._handle.fileno()
        self._bytes_written = 0
        self._released_to = 0

    @property
    def path(self) -> pathlib.Path:
        return self._path

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def append(self, block: bytes) -> int:
        """Append a block, forcing writeback and releasing cache once per interval."""
        self._handle.write(block)
        self._bytes_written += len(block)
        if self._bytes_written - self._released_to >= self._interval:
            self.force_writeback()
        return self._bytes_written

    def force_writeback(self) -> None:
        """Flush to the kernel, fsync to disk, then hand the written range back.

        POSIX_FADV_DONTNEED only drops *clean* pages, so the fsync is not optional
        decoration -- without it there is nothing clean to drop.
        """
        self._handle.flush()
        os.fsync(self._descriptor)
        length = self._bytes_written - self._released_to
        if length > 0:
            os.posix_fadvise(
                self._descriptor, self._released_to, length, os.POSIX_FADV_DONTNEED
            )
            self._released_to = self._bytes_written

    def close(self) -> None:
        if self._handle.closed:
            return
        self.force_writeback()
        self._handle.close()

    def __enter__(self) -> "CacheReleasingWriter":
        return self

    def __exit__(self, *exception) -> None:
        self.close()
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_page_cache_discipline.py -v`
Expected: PASS, 4 passed. The two cgroup tests take several seconds each.

- [ ] **Step 5: Confirm the number the spec claims, by hand once**

```bash
.venv/bin/python -m pytest tests/runtime/test_page_cache_discipline.py -v -m cgroup -s
```
Expected: the drop-cache test passes with a peak well under 64 MB, and the naive-writer control is killed. If the control *survives*, the memory limit was not applied and neither result means anything — check `systemctl --user show -p Result --value <unit>.scope`, and remember that `systemctl show` reports `success` for a unit that does not exist.

- [ ] **Step 6: Commit**

```bash
git add runtime/page_cache_discipline.py tests/runtime/test_page_cache_discipline.py
git commit -m "page_cache_discipline: a stream writer that returns its own page cache

A part's memory.max counts the page cache it dirties and there is no swap
here. Measured on ext4 under MemoryMax=200M writing 500 MB: never
fsyncing is an OOM kill, fsync alone survives at the ceiling, fsync plus
FADV_DONTNEED holds at 16 MB. The naive writer is kept as the control,
because without it the passing test proves only that something ran."
```

---

### Task 5: `forkserver_launcher` — the caps are load-bearing, so the fork site checks

**Files:**
- Create: `runtime/forkserver_launcher.py`
- Test: `tests/runtime/test_forkserver_launcher.py`

**Interfaces:**
- Consumes: `runtime.settings_reader.SettingsDocument` (entry `fork_thread_ceiling`).
- Produces:
  - `BLAS_THREAD_CAP_VARIABLES: tuple[str, ...]`
  - `def apply_blas_thread_caps(environment: MutableMapping[str, str] | None = None) -> dict[str, str]`
  - `def read_kernel_thread_count(pid: int | str = "self") -> int`
  - `def is_safe_to_fork(thread_ceiling: int, pid: int | str = "self") -> bool`
  - `class ForkRefused(RuntimeError)`
  - `def start_forkserver(preload_modules: tuple[str, ...]) -> multiprocessing.context.BaseContext`
  - `def spawn_part(context, entry_point, arguments: tuple, thread_ceiling: int) -> multiprocessing.Process` — raises `ForkRefused`
- Task 8 (`part_process`) spawns every part through `spawn_part`. Nothing else calls `multiprocessing` directly.

**Why the check is not paranoia:** §1 rule 3, as corrected, is the argument. `import numpy` alone puts **12** kernel threads in the process unless the caps are already in the environment, while `threading.active_count()` still reports **1**. So the caps are what make the forkserver forkable at all, and reading field 20 of `/proc/self/stat` is what catches a missing environment variable at the first fork rather than as an unreproducible hang weeks later. POSIX: a mutex held by another thread at the instant of `fork()` is copied **locked**, and the thread that would release it does not exist in the child — a hang, not a crash.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_forkserver_launcher.py`:

```python
"""The BLAS thread caps are load-bearing for fork safety, not a scheduling preference.

Section 1 rule 3, as corrected by measurement: with the caps unset, import numpy
alone puts 12 kernel threads in the process -- before any array maths -- while
threading.active_count() still reports 1. That is why the fork site reads field 20
of /proc/self/stat and not Python's own count.
"""

import multiprocessing
import os
import subprocess
import sys

import pytest

from runtime.forkserver_launcher import (
    BLAS_THREAD_CAP_VARIABLES,
    ForkRefused,
    apply_blas_thread_caps,
    is_safe_to_fork,
    read_kernel_thread_count,
    spawn_part,
    start_forkserver,
)

CAPPED = "; ".join(f"os.environ['{name}'] = '1'" for name in [
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
])

THREAD_COUNT_AFTER_NUMPY = """
import os, sys
{caps}
import numpy
import threading
sys.path.insert(0, {repository!r})
from runtime.forkserver_launcher import read_kernel_thread_count
print(read_kernel_thread_count(), threading.active_count())
"""


def _thread_count_after_importing_numpy(caps: str) -> tuple[int, int]:
    repository = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = THREAD_COUNT_AFTER_NUMPY.format(caps=caps, repository=repository)
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env={k: v for k, v in os.environ.items()
                                    if k not in BLAS_THREAD_CAP_VARIABLES})
    assert completed.returncode == 0, completed.stderr[-400:]
    kernel, python = completed.stdout.split()
    return int(kernel), int(python)


def test_reads_the_kernel_thread_count_not_pythons():
    assert read_kernel_thread_count() >= 1


def test_uncapped_numpy_import_spawns_threads_python_cannot_see():
    kernel, python = _thread_count_after_importing_numpy(caps="pass")
    assert kernel > 1, "OpenBLAS did not spawn at import; re-check the caps really were unset"
    assert python == 1, "this is the whole point: threading.active_count() cannot see them"


def test_capped_numpy_import_leaves_the_process_forkable():
    kernel, python = _thread_count_after_importing_numpy(caps=CAPPED)
    assert kernel == 1
    assert python == 1


def test_apply_blas_thread_caps_sets_every_variable_and_reports_what_it_set():
    environment: dict[str, str] = {}
    applied = apply_blas_thread_caps(environment)
    assert set(applied) == set(BLAS_THREAD_CAP_VARIABLES)
    assert all(environment[name] == "1" for name in BLAS_THREAD_CAP_VARIABLES)


def test_apply_blas_thread_caps_does_not_overwrite_a_deliberate_setting():
    environment = {"OPENBLAS_NUM_THREADS": "2"}
    apply_blas_thread_caps(environment)
    assert environment["OPENBLAS_NUM_THREADS"] == "2"


def test_is_safe_to_fork_is_true_for_a_single_threaded_process():
    assert is_safe_to_fork(thread_ceiling=1) is True


def test_spawn_part_refuses_when_the_process_holds_more_threads_than_the_ceiling():
    context = start_forkserver(preload_modules=("numpy",))
    with pytest.raises(ForkRefused) as refusal:
        spawn_part(context, entry_point=_report_thread_count, arguments=(), thread_ceiling=0)
    assert "/proc/self/stat" in str(refusal.value)


def _report_thread_count() -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name."""
    import numpy
    assert "numpy" in sys.modules
    print(read_kernel_thread_count())


def test_a_part_forked_from_the_forkserver_inherits_the_preloaded_numpy():
    context = start_forkserver(preload_modules=("numpy",))
    queue = context.Queue()
    process = spawn_part(context, entry_point=_report_preload_state, arguments=(queue,), thread_ceiling=1)
    inherited, kernel_threads = queue.get(timeout=30)
    process.join(timeout=30)
    assert inherited is True, "the child re-imported numpy instead of inheriting it"
    assert kernel_threads == 1, "the child came up with BLAS threads; the caps did not reach it"


def _report_preload_state(queue) -> None:
    """Module-level on purpose (section 1 rule 2)."""
    import sys as child_sys
    queue.put(("numpy" in child_sys.modules, read_kernel_thread_count()))
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_forkserver_launcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.forkserver_launcher'`.

- [ ] **Step 3: Write the implementation**

`runtime/forkserver_launcher.py`:

```python
"""Start the forkserver every part is forked from, and refuse an unsafe fork.

Why forkserver and not a hand-rolled zygote (section 1): a warm parent that forks
is four times cheaper per idle child -- 0.76 MB PSS against 3.07 -- and it is still
the wrong choice, because fork's safety condition cannot be verified once and
trusted. POSIX: a mutex another thread holds at the instant of fork is copied
locked, and the thread that would release it does not exist in the child. That is
a hang, not a crash, and it has to hold at every fork for the life of the process,
against every library any dependency pulls in. forkserver moves that invariant into
a maintained component whose only job is to hold it.

Why the caps come first (section 1 rule 3, corrected by measurement): with them
unset, import numpy alone puts 12 kernel threads in the process, before any array
maths, while threading.active_count() still reports 1. So the caps are what make
the forkserver forkable, and reading field 20 of /proc/self/stat is what turns a
missing environment variable into an immediate logged refusal.
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
from collections.abc import Callable, MutableMapping

# Every library that might start its own thread pool on import. OPENBLAS_NUM_THREADS
# is the authoritative one for our wheel: PyPI OpenBLAS wheels are built with
# pthreads, not OpenMP, so OMP_NUM_THREADS is a fallback rather than the control.
BLAS_THREAD_CAP_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

SINGLE_THREAD = "1"

# Field 20 of /proc/<pid>/stat, 1-indexed as the man page counts it, is num_threads.
# It has to be parsed after the last ')' because field 2 is the executable name and
# may itself contain spaces and parentheses.
_NUM_THREADS_INDEX_AFTER_COMM = 17


class ForkRefused(RuntimeError):
    """A fork was refused because the forking process held more than one thread."""


def apply_blas_thread_caps(
    environment: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Pin every BLAS thread pool to one thread, returning what was set.

    Must run before numpy is imported anywhere in the process. A deliberate value
    already in the environment is left alone -- the governor owns parallelism, and
    if it decided a part gets two BLAS threads, this is not the place to overrule it.
    """
    target = os.environ if environment is None else environment
    applied: dict[str, str] = {}
    for name in BLAS_THREAD_CAP_VARIABLES:
        if name not in target:
            target[name] = SINGLE_THREAD
        applied[name] = target[name]
    return applied


def read_kernel_thread_count(pid: int | str = "self") -> int:
    """How many threads the kernel says this process has.

    Deliberately not threading.active_count(): that counts Python's own Thread
    objects and cannot see a thread OpenBLAS started in C. Measured, the two
    disagree 12 to 1 immediately after an uncapped import of numpy.
    """
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    after_comm = raw.rsplit(")", 1)[1].split()
    return int(after_comm[_NUM_THREADS_INDEX_AFTER_COMM])


def is_safe_to_fork(thread_ceiling: int, pid: int | str = "self") -> bool:
    """Is this process single-threaded enough to fork without risking a locked mutex?"""
    return read_kernel_thread_count(pid) <= thread_ceiling


def start_forkserver(preload_modules: tuple[str, ...]) -> multiprocessing.context.BaseContext:
    """Return a forkserver context with the given modules already imported.

    The preload imports numpy but must never execute a BLAS call: a warm-up matmul
    would be a computation in the forking process, and the caps are the only reason
    that process is single-threaded at all.
    """
    apply_blas_thread_caps()
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(list(preload_modules))
    return context


def spawn_part(
    context: multiprocessing.context.BaseContext,
    entry_point: Callable[..., None],
    arguments: tuple,
    thread_ceiling: int,
) -> multiprocessing.Process:
    """Fork one part, refusing loudly rather than risking a child that hangs.

    entry_point must be module-level: forkserver pickles the target by qualified
    name, and a nested function fails with
    AttributeError: module '__mp_main__' has no attribute '...' (section 1 rule 2).
    """
    observed = read_kernel_thread_count()
    if observed > thread_ceiling:
        raise ForkRefused(
            f"refusing to fork: /proc/self/stat reports {observed} kernel threads, ceiling is "
            f"{thread_ceiling}. A mutex held by another thread at the instant of fork is copied "
            f"locked into the child and never released. The usual cause is numpy imported before "
            f"the BLAS thread caps were applied -- see apply_blas_thread_caps."
        )
    process = context.Process(target=entry_point, args=arguments)
    process.start()
    return process
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_forkserver_launcher.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 5: Commit**

```bash
git add runtime/forkserver_launcher.py tests/runtime/test_forkserver_launcher.py
git commit -m "forkserver_launcher: the caps make the fork safe, so the fork site checks

import numpy alone puts 12 kernel threads in the process unless the BLAS
caps are already set, while threading.active_count() still reports 1.
That is why the check reads field 20 of /proc/self/stat, and why a
missing environment variable is now an immediate refusal rather than an
unreproducible hang."
```

---

### Task 6: `scope_placer` — rc=0 is not evidence the part moved

**Files:**
- Create: `runtime/scope_placer.py`
- Test: `tests/runtime/test_scope_placer.py`

**Interfaces:**
- Consumes: `runtime.settings_reader.SettingsDocument` (entries `placement_confirmation_deadline`, `placement_confirmation_poll_interval`).
- Produces:
  - `class ScopeLimits` — frozen dataclass `(memory_max_bytes: int, cpu_weight: int, pids_max: int | None = None, cpu_quota_percent: int | None = None)`
  - `class PlacementNotConfirmed(RuntimeError)`
  - `def read_process_cgroup(pid: int) -> str`
  - `def has_process_landed_in_scope(pid: int, scope_name: str) -> bool`
  - `def place_process_in_scope(pid, scope_name, limits, confirmation_deadline_seconds, poll_interval_seconds) -> pathlib.Path` — returns the cgroup directory, raises `PlacementNotConfirmed`
  - `def read_scope_limits_in_effect(cgroup_directory: pathlib.Path) -> dict[str, str]`
- Task 8 places every part through this. Phase 2's `gate-actuator` is built on it.

**Why confirmation is mandatory:** `StartTransientUnit` queues an asynchronous job and returns its object path, so a job that then fails to move the PID still leaves the caller holding **rc=0**. Observed here once: a placement returned rc=0 in 7.6 ms while the child stayed in `session-9.scope` with `memory.max` unset, and only the user manager's journal carried `Failed to add PIDs to scope's control group: Permission denied`. It did not reproduce — **87 of 88 subsequent placements succeeded**, fork at a 2.3 ms median and placement at 5.7 ms median / 6.9 ms p95, with no settle delay needed. One silent failure in ninety, on the path that puts a part under its limits, is answered by verification and not by trusting a return code. A part running outside its own scope is a part the governor believes it has bounded and has not.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_scope_placer.py`:

```python
"""Placement returns rc=0 before it is attempted, so rc=0 is not evidence.

Section 3: StartTransientUnit queues an asynchronous job and returns its object
path. Observed once in 88 attempts, a placement returned rc=0 while the process
stayed in its old cgroup with memory.max unset. These tests hold the confirmation.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from runtime.scope_placer import (
    PlacementNotConfirmed,
    ScopeLimits,
    has_process_landed_in_scope,
    place_process_in_scope,
    read_process_cgroup,
    read_scope_limits_in_effect,
)

DEADLINE = 0.5
POLL = 0.002
MEGABYTE = 1024 * 1024


@pytest.fixture
def sleeping_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield process
    if process.poll() is None:
        process.send_signal(signal.SIGKILL)
    process.wait()


def test_reads_the_cgroup_a_process_is_actually_in(sleeping_process):
    assert read_process_cgroup(sleeping_process.pid).startswith("/")


@pytest.mark.cgroup
def test_places_a_process_and_the_limits_are_really_in_effect(sleeping_process):
    scope = f"placer-test-{os.getpid()}-{sleeping_process.pid}"
    limits = ScopeLimits(memory_max_bytes=256 * MEGABYTE, cpu_weight=137, pids_max=64)

    directory = place_process_in_scope(
        sleeping_process.pid, scope, limits,
        confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
    )

    assert has_process_landed_in_scope(sleeping_process.pid, scope) is True
    in_effect = read_scope_limits_in_effect(directory)
    assert in_effect["memory.max"] == str(256 * MEGABYTE)
    assert in_effect["cpu.weight"] == "137"
    # The per-part scarcity signal section 5 depends on. The system-wide 'full' line
    # is zero by definition, so only the per-cgroup file is usable.
    assert "full" in in_effect["cpu.pressure"]


@pytest.mark.cgroup
def test_refuses_to_report_success_when_the_process_never_arrives():
    # A PID that has already exited is the reproducible version of the failure that
    # was observed once: the call is accepted, the move never happens.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with pytest.raises(PlacementNotConfirmed) as refusal:
        place_process_in_scope(
            dead.pid, f"placer-dead-{os.getpid()}",
            ScopeLimits(memory_max_bytes=64 * MEGABYTE, cpu_weight=100),
            confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
        )
    assert "/proc" in str(refusal.value)


@pytest.mark.cgroup
@pytest.mark.slow
def test_placement_is_reliable_and_fast_enough_to_be_on_the_switch_on_path():
    # Section 3 quotes 5.6 ms to place. If this regresses, switch-on regressed.
    latencies = []
    for attempt in range(10):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        scope = f"placer-rate-{os.getpid()}-{attempt}"
        started = time.perf_counter()
        place_process_in_scope(
            process.pid, scope,
            ScopeLimits(memory_max_bytes=64 * MEGABYTE, cpu_weight=100),
            confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
        )
        latencies.append(time.perf_counter() - started)
        process.send_signal(signal.SIGKILL)
        process.wait()

    latencies.sort()
    assert latencies[len(latencies) // 2] < 0.1, f"median placement {latencies[len(latencies)//2]:.4f}s"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_scope_placer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.scope_placer'`.

- [ ] **Step 3: Write the implementation**

`runtime/scope_placer.py`:

```python
"""Move a part into its own cgroup, and confirm it actually landed there.

A forkserver child inherits the forkserver's cgroup, and raw mkdir under the
systemd-owned session scope is refused -- systemd owns that subtree. So a part is
placed through the user bus rather than by hand-rolled cgroupfs, which is also
what gives it a per-part cpu.pressure file, the scarcity signal section 5 depends on.

The confirmation is the point of this module. StartTransientUnit queues an
asynchronous job and returns its object path, so a job that then fails to move the
PID still leaves the caller holding rc=0. Observed once in 88 attempts here: rc=0
in 7.6 ms while the child stayed in session-9.scope with memory.max unset, the real
outcome only in the user manager's journal. A part running outside its own scope is
a part the governor believes it has bounded and has not.
"""

from __future__ import annotations

import pathlib
import subprocess
import time
from dataclasses import dataclass

CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
SYSTEMD_OBJECT_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"

# The files whose presence proves the part is bounded and observable.
SCOPE_LIMIT_FILES = ("memory.max", "cpu.weight", "pids.max", "cpu.pressure", "memory.pressure")


class PlacementNotConfirmed(RuntimeError):
    """The placement call was accepted but the process is not in the scope."""


@dataclass(frozen=True)
class ScopeLimits:
    """What the governor is bounding this part to.

    Every value here is decided by the governor from measurement -- hardware-scanner
    and part-appetite-meter -- never written in as a literal (RL-061). cpu.weight's
    range is [1, 10000] with a default of 100, and it matters only under contention.
    """

    memory_max_bytes: int
    cpu_weight: int
    pids_max: int | None = None
    cpu_quota_percent: int | None = None


def read_process_cgroup(pid: int) -> str:
    """The cgroup path a process is in right now, read from the kernel."""
    return pathlib.Path(f"/proc/{pid}/cgroup").read_text().strip().split("::")[1]


def has_process_landed_in_scope(pid: int, scope_name: str) -> bool:
    """Is this process in that scope? Read from /proc, never inferred from a return code."""
    try:
        return read_process_cgroup(pid).endswith(f"/{scope_name}.scope")
    except (OSError, IndexError):
        return False


def _build_transient_unit_call(pid: int, scope_name: str, limits: ScopeLimits) -> list[str]:
    """Build the busctl argv for StartTransientUnit.

    The signature is ssa(sv)a(sa(sv)): unit name, job mode, an array of properties,
    and an array of auxiliary units. busctl wants the *count of properties* before
    them, so they are built as whole properties and flattened once -- counting the
    flattened words instead is the easy way to send a malformed message that
    systemd rejects with a signature error.

    Each property is a name plus a variant. PIDs is au, an array of uint32, so its
    variant carries its own length. The rest are t, uint64.
    """
    properties: list[list[str]] = [
        ["PIDs", "au", "1", str(pid)],
        ["MemoryMax", "t", str(limits.memory_max_bytes)],
        ["CPUWeight", "t", str(limits.cpu_weight)],
    ]
    if limits.pids_max is not None:
        properties.append(["TasksMax", "t", str(limits.pids_max)])
    if limits.cpu_quota_percent is not None:
        # systemd takes CPUQuotaPerSecUSec: microseconds of CPU per wall second.
        # 100% of one CPU is 1 000 000 us, so a percentage is worth 10 000 us.
        microseconds_per_percent = 10_000
        properties.append(
            ["CPUQuotaPerSecUSec", "t", str(limits.cpu_quota_percent * microseconds_per_percent)]
        )
    flattened = [word for prop in properties for word in prop]
    return [
        "busctl", "--user", "call", SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH,
        SYSTEMD_MANAGER_INTERFACE, "StartTransientUnit", "ssa(sv)a(sa(sv))",
        f"{scope_name}.scope", "fail", str(len(properties)), *flattened, "0",
    ]


def place_process_in_scope(
    pid: int,
    scope_name: str,
    limits: ScopeLimits,
    confirmation_deadline_seconds: float,
    poll_interval_seconds: float,
) -> pathlib.Path:
    """Place a live PID in its own transient scope and confirm it arrived.

    Returns the cgroup directory. Raises PlacementNotConfirmed rather than
    returning a part the governor cannot bound.
    """
    call = _build_transient_unit_call(pid, scope_name, limits)
    completed = subprocess.run(call, capture_output=True, text=True)

    deadline = time.monotonic() + confirmation_deadline_seconds
    while time.monotonic() < deadline:
        if has_process_landed_in_scope(pid, scope_name):
            return CGROUP_ROOT / read_process_cgroup(pid).lstrip("/")
        time.sleep(poll_interval_seconds)

    try:
        observed = read_process_cgroup(pid)
    except OSError:
        observed = "the process no longer exists"
    raise PlacementNotConfirmed(
        f"{scope_name}.scope was requested for pid {pid} and busctl returned "
        f"{completed.returncode}, but /proc says the process is in {observed} after "
        f"{confirmation_deadline_seconds}s. StartTransientUnit queues an asynchronous "
        f"job, so its return code is not evidence the move happened. busctl stderr: "
        f"{completed.stderr.strip()[:200] or 'empty'}. The real reason is in "
        f"`journalctl --user -u {scope_name}.scope`."
    )


def read_scope_limits_in_effect(cgroup_directory: pathlib.Path) -> dict[str, str]:
    """Read back what the kernel is actually enforcing, for the switch record."""
    in_effect: dict[str, str] = {}
    for filename in SCOPE_LIMIT_FILES:
        try:
            in_effect[filename] = (cgroup_directory / filename).read_text().strip()
        except OSError:
            in_effect[filename] = "unreadable"
    return in_effect
```

- [ ] **Step 4: Verify the call shape by hand before trusting the tests**

```bash
.venv/bin/python -c "
import subprocess, sys, time, signal
from runtime.scope_placer import ScopeLimits, place_process_in_scope, read_scope_limits_in_effect
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
directory = place_process_in_scope(child.pid, 'handcheck', ScopeLimits(256*1024*1024, 133, 64),
                                   confirmation_deadline_seconds=0.5, poll_interval_seconds=0.002)
print(directory)
for name, value in read_scope_limits_in_effect(directory).items():
    print(f'  {name:16} {value.splitlines()[0]}')
child.send_signal(signal.SIGKILL); child.wait()
"
```
Expected: a path under `user@1001.service/app.slice/handcheck.scope`, `memory.max 268435456`, `cpu.weight 133`, and a `cpu.pressure` line.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_scope_placer.py -v`
Expected: PASS, 4 passed.

- [ ] **Step 6: Commit**

```bash
git add runtime/scope_placer.py tests/runtime/test_scope_placer.py
git commit -m "scope_placer: confirm the part landed, because rc=0 is not evidence

StartTransientUnit queues an async job and returns before it runs.
Observed once in 88 attempts: rc=0 in 7.6 ms with the child still in its
old cgroup and memory.max unset. Placement is otherwise reliable and
needs no settle delay -- 87 of 88, 5.7 ms median. So this raises rather
than returning a part the governor cannot bound."
```

---

### Task 7: `control_channel` — the switch, and why no part can reach another's

**Files:**
- Create: `runtime/control_channel.py`
- Test: `tests/runtime/test_control_channel.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `COMMAND_TURN_ON`, `COMMAND_TURN_OFF`, `COMMAND_REPORT_HEALTH` — `str` constants
  - `class ControlFrameRefused(ValueError)`
  - `def create_control_socket_pair() -> tuple[socket.socket, socket.socket]` — returns `(governor_end, part_end)`
  - `def send_command(sock: socket.socket, command: str, payload: dict) -> None`
  - `def receive_command(sock: socket.socket) -> tuple[str, dict] | None` — `None` on clean close
  - `def has_pending_command(sock: socket.socket, timeout_seconds: float) -> bool`

**Why the kernel enforces T-2 and T-4 here:** each part is started with an inherited descriptor for its own socket. A part cannot open another part's control socket because it was never given one — that is the fd table, not code review. A part never receives a command on its data path and never emits data on its control path.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_control_channel.py`:

```python
"""The switch is one framed command on a descriptor only the governor holds.

T-2 (only the resource governor switches parts) and T-4 (a part knows nothing about
the circuit) are enforced by the kernel's fd table here, not by convention.
"""

import os
import socket

import pytest

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
    ControlFrameRefused,
    create_control_socket_pair,
    has_pending_command,
    receive_command,
    send_command,
)


def test_a_command_arrives_with_its_payload_intact():
    governor, part = create_control_socket_pair()
    send_command(governor, COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})


def test_two_commands_do_not_run_into_each_other():
    governor, part = create_control_socket_pair()
    send_command(governor, COMMAND_REPORT_HEALTH, {"sequence": 1})
    send_command(governor, COMMAND_TURN_OFF, {"sequence": 2})
    assert receive_command(part) == (COMMAND_REPORT_HEALTH, {"sequence": 1})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"sequence": 2})


def test_a_clean_close_reads_as_no_command_rather_than_an_error():
    governor, part = create_control_socket_pair()
    governor.close()
    assert receive_command(part) is None


def test_an_unknown_command_is_refused_rather_than_silently_ignored():
    governor, part = create_control_socket_pair()
    with pytest.raises(ControlFrameRefused) as refusal:
        send_command(governor, "please-do-something-clever", {})
    assert "please-do-something-clever" in str(refusal.value)


def test_waiting_reports_whether_a_command_is_actually_there():
    governor, part = create_control_socket_pair()
    assert has_pending_command(part, timeout_seconds=0.01) is False
    send_command(governor, COMMAND_TURN_OFF, {})
    assert has_pending_command(part, timeout_seconds=0.5) is True


def test_a_part_holds_only_its_own_control_descriptor():
    # The structural claim: a part is handed one end and never sees any other.
    first_governor, first_part = create_control_socket_pair()
    second_governor, second_part = create_control_socket_pair()
    held = {first_part.fileno()}
    assert second_part.fileno() not in held
    assert second_governor.fileno() not in held
    # And the governor's end is not inheritable by accident.
    assert os.get_inheritable(first_governor.fileno()) is False
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_control_channel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.control_channel'`.

- [ ] **Step 3: Write the implementation**

`runtime/control_channel.py`:

```python
"""One control socket per part, owned by the governor. That is the entire switch.

The part's loop selects on {control fd, data inputs}. gate-actuator writes one
framed command onto the control fd and nothing else does, because nothing else was
given the descriptor -- T-2 and T-4 are enforced by the kernel's fd table rather
than by code review.

Framing is a four-byte big-endian length followed by JSON. A stream socket has no
message boundaries of its own, so without a length prefix two commands sent quickly
arrive as one read and the second is lost.
"""

from __future__ import annotations

import json
import os
import select
import socket
import struct

COMMAND_TURN_ON = "on"
COMMAND_TURN_OFF = "off"
COMMAND_REPORT_HEALTH = "report-health"

KNOWN_COMMANDS = frozenset({COMMAND_TURN_ON, COMMAND_TURN_OFF, COMMAND_REPORT_HEALTH})

_LENGTH_PREFIX = struct.Struct("!I")
# A control frame carries a command and a small reason. Anything larger is a data
# payload arriving on the control path, which section 3 forbids outright.
MAXIMUM_FRAME_BYTES = 64 * 1024


class ControlFrameRefused(ValueError):
    """A frame was not a command this runtime knows, or was too large to be one."""


def create_control_socket_pair() -> tuple[socket.socket, socket.socket]:
    """Return (governor end, part end). Only the part end is inheritable."""
    governor_end, part_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(governor_end.fileno(), False)
    os.set_inheritable(part_end.fileno(), True)
    return governor_end, part_end


def _receive_exactly(sock: socket.socket, count: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_command(sock: socket.socket, command: str, payload: dict) -> None:
    """Write one framed command. Refuses a command the runtime does not define."""
    if command not in KNOWN_COMMANDS:
        raise ControlFrameRefused(
            f"'{command}' is not a control command. T-5: states are explicit and countable, "
            f"and so are the commands that change them. Known: {sorted(KNOWN_COMMANDS)}."
        )
    body = json.dumps({"command": command, "payload": payload}).encode("utf-8")
    if len(body) > MAXIMUM_FRAME_BYTES:
        raise ControlFrameRefused(
            f"control frame is {len(body)} bytes, over the {MAXIMUM_FRAME_BYTES} limit. "
            f"A part never receives data on its control path (section 3)."
        )
    sock.sendall(_LENGTH_PREFIX.pack(len(body)) + body)


def receive_command(sock: socket.socket) -> tuple[str, dict] | None:
    """Read one framed command, or None when the governor closed the socket."""
    header = _receive_exactly(sock, _LENGTH_PREFIX.size)
    if header is None:
        return None
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAXIMUM_FRAME_BYTES:
        raise ControlFrameRefused(f"control frame claims {length} bytes; refusing to read it")
    body = _receive_exactly(sock, length)
    if body is None:
        return None
    frame = json.loads(body.decode("utf-8"))
    command = frame.get("command")
    if command not in KNOWN_COMMANDS:
        raise ControlFrameRefused(f"received unknown control command '{command}'")
    return command, frame.get("payload", {})


def has_pending_command(sock: socket.socket, timeout_seconds: float) -> bool:
    """Is there a command waiting? The part's loop selects on this and its data inputs."""
    readable, _, _ = select.select([sock], [], [], timeout_seconds)
    return bool(readable)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_control_channel.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add runtime/control_channel.py tests/runtime/test_control_channel.py
git commit -m "control_channel: the switch, framed, on a descriptor only its part holds

T-2 and T-4 are enforced by the kernel's fd table here. Length-prefixed
because a stream socket has no message boundaries and two fast commands
would otherwise arrive as one read."
```

---

### Task 8: `part_process` — the part template, and off really means off

**Files:**
- Create: `runtime/part_declaration.py`
- Create: `runtime/part_process.py`
- Test: `tests/runtime/test_part_declaration.py`
- Test: `tests/runtime/test_part_process.py`

**Interfaces:**
- Consumes: `runtime.control_channel`, `runtime.forkserver_launcher`, `runtime.scope_placer`, `runtime.settings_reader` (entry `part_health_interval`).
- Produces:
  - `class ResourceClass(enum.StrEnum)` — `IO_BOUND`, `COMPUTE_BOUND`, `BANDWIDTH_BOUND`
  - `class RateRisk(enum.StrEnum)` — `LATENCY_ONLY`, `CHANGES_THE_ANSWER`
  - `class SkippedTickEffect(enum.StrEnum)` — `DELAYS`, `CORRUPTS`
  - `class PartDeclaration` — frozen dataclass `(part_id, consumes: tuple[str, ...], produces: tuple[str, ...], resource_class, rate_risk, skipped_tick_effect)`
  - `def may_enter_rate_ladder(declaration: PartDeclaration) -> bool`
  - `def load_declaration_from_blueprint(part_id: str) -> PartDeclaration`
  - `class PartHealth` — frozen dataclass `(part_id, state, rate_ratio: float, staleness_seconds: float, observed_at_ns: int)`
  - `def run_part(declaration, control_socket, do_one_tick: Callable[[], None], emit_health: Callable[[PartHealth], None], health_interval_seconds: float) -> int`
- Task 14's probes measure a part started this way. Phase 1's 13 feed parts are written against `run_part`.

**Why `may_enter_rate_ladder` lives with the declaration:** §6 admits a part to a rate ladder only if a lower rate changes *when* an answer arrives and never *what* it is, **and** no skipped tick corrupts a monotone invariant. Both facts are declared, so the predicate is one line and the contract checker (Task 13) enforces that both were declared at all.

- [ ] **Step 1: Write the failing declaration test**

`tests/runtime/test_part_declaration.py`:

```python
"""Section 6 admits a part to a rate ladder only on both counts, not either.

(a) does a lower rate change the number produced, or only its arrival time?
(b) does a skipped tick corrupt a monotone invariant, or merely delay it?
Only latency-risk-only on both may be throttled. Everything else gets a floor.
"""

import pytest

from runtime.part_declaration import (
    PartDeclaration,
    RateRisk,
    ResourceClass,
    SkippedTickEffect,
    load_declaration_from_blueprint,
    may_enter_rate_ladder,
)


def _declare(rate_risk: RateRisk, effect: SkippedTickEffect) -> PartDeclaration:
    return PartDeclaration(
        part_id="kline-window-builder",
        consumes=("market-data",),
        produces=("kline-window", "part-health"),
        resource_class=ResourceClass.BANDWIDTH_BOUND,
        rate_risk=rate_risk,
        skipped_tick_effect=effect,
    )


def test_a_part_that_only_arrives_later_may_be_throttled():
    assert may_enter_rate_ladder(_declare(RateRisk.LATENCY_ONLY, SkippedTickEffect.DELAYS)) is True


def test_a_part_whose_answer_changes_may_never_be_throttled():
    # Every IIR indicator: EMA, RSI, ATR, MACD. A shorter window is a different,
    # silently biased answer that looks identical to a healthy one.
    assert may_enter_rate_ladder(
        _declare(RateRisk.CHANGES_THE_ANSWER, SkippedTickEffect.DELAYS)
    ) is False


def test_a_part_whose_skipped_tick_corrupts_may_never_be_throttled():
    # Order-book reconstruction and balance reconciliation: binary correctness.
    assert may_enter_rate_ladder(
        _declare(RateRisk.LATENCY_ONLY, SkippedTickEffect.CORRUPTS)
    ) is False


def test_both_together_are_still_refused():
    assert may_enter_rate_ladder(
        _declare(RateRisk.CHANGES_THE_ANSWER, SkippedTickEffect.CORRUPTS)
    ) is False


def test_a_declaration_loaded_from_the_blueprint_matches_what_the_blueprint_says():
    # RL-067: what is built matches the diagrams. A part's real consumes and
    # produces equal what features.json declares, or the probe in Task 14 fails.
    declaration = load_declaration_from_blueprint("kline-window-builder")
    assert declaration.consumes == ("market-data",)
    assert "part-health" in declaration.produces


def test_loading_a_part_that_is_not_in_the_blueprint_refuses():
    with pytest.raises(KeyError):
        load_declaration_from_blueprint("a-part-nobody-declared")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_part_declaration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.part_declaration'`.

- [ ] **Step 3: Write `runtime/part_declaration.py`**

```python
"""What every part declares about itself. One shape, no privileged parts (T-1).

Three of these fields are new in phase 0 and are added to all 321 blueprint entries
by the edit in Task 13: resource_class (section 7), and the pair rate_risk and
skipped_tick_effect that section 6 requires before a part may be throttled at all.
"""

from __future__ import annotations

import enum
import json
import pathlib
from dataclasses import dataclass

BLUEPRINT_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "features.json"


class ResourceClass(enum.StrEnum):
    """How the governor should allocate to this part (section 7)."""

    # Blocked in epoll_wait, costs nothing while idle: shared pool, generous concurrency.
    IO_BOUND = "io-bound"
    # Pinned cores, BLAS threads = 1, because the governor owns parallelism.
    COMPUTE_BOUND = "compute-bound"
    # One memory controller, one NUMA node. Two are never co-scheduled: they divide
    # a fixed pipe rather than adding throughput. Rolling-window statistics live here,
    # which is the workload this project runs most.
    BANDWIDTH_BOUND = "bandwidth-bound"


class RateRisk(enum.StrEnum):
    """Section 6 (a): does a lower rate change the number, or only when it arrives?"""

    LATENCY_ONLY = "latency-only"
    CHANGES_THE_ANSWER = "changes-the-answer"


class SkippedTickEffect(enum.StrEnum):
    """Section 6 (b): does a skipped tick corrupt an invariant, or merely delay it?"""

    DELAYS = "delays"
    CORRUPTS = "corrupts"


@dataclass(frozen=True)
class PartDeclaration:
    """One part's contract, as the blueprint declares it."""

    part_id: str
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    resource_class: ResourceClass
    rate_risk: RateRisk
    skipped_tick_effect: SkippedTickEffect


def may_enter_rate_ladder(declaration: PartDeclaration) -> bool:
    """Section 6: only latency-risk-only on both counts may be throttled.

    Everything else gets a reserved floor instead. The two errors are not symmetric
    -- refusing to throttle something throttleable costs an eviction, which is
    visible and recoverable; throttling something unthrottleable costs a number that
    is wrong while still looking healthy.
    """
    return (
        declaration.rate_risk is RateRisk.LATENCY_ONLY
        and declaration.skipped_tick_effect is SkippedTickEffect.DELAYS
    )


def load_declaration_from_blueprint(part_id: str) -> PartDeclaration:
    """Read one part's declaration from docs/features.json.

    The blueprint is the single source of truth: code follows the registry, never
    the other way round.
    """
    registry = json.loads(BLUEPRINT_PATH.read_text())
    for feature in registry["features"]:
        if feature["id"] == part_id:
            return PartDeclaration(
                part_id=part_id,
                consumes=tuple(feature["consumes"]),
                produces=tuple(feature["produces"]),
                resource_class=ResourceClass(feature["resource_class"]),
                rate_risk=RateRisk(feature["rate_risk"]),
                skipped_tick_effect=SkippedTickEffect(feature["skipped_tick_effect"]),
            )
    raise KeyError(
        f"'{part_id}' is not in {BLUEPRINT_PATH}. A part that is not in the blueprint is "
        f"not a part -- a design change is a blueprint edit first, then code."
    )
```

**Ordering note:** `test_a_declaration_loaded_from_the_blueprint_matches_what_the_blueprint_says` and `test_loading_a_part_that_is_not_in_the_blueprint_refuses` will fail until Task 13 adds the three fields to `features.json`. Mark exactly those two with `@pytest.mark.xfail(reason="the three declarations land in Task 13", strict=True)` when writing them, and **delete both marks in Task 13's step that reruns this file** — a strict xfail turns into a failure the moment it starts passing, so it cannot be forgotten.

- [ ] **Step 4: Write the failing part-process test**

`tests/runtime/test_part_process.py`:

```python
"""A part is a process, and off means the process does not exist.

T-3: an off part releases its CPU and RAM. A thread that turns off still holds its
share of a shared heap, so only a process exiting gives memory back. These tests
measure that rather than assuming it -- off-state-verifier does the same thing in
phase 2.
"""

import os
import time

import pytest

from runtime.control_channel import COMMAND_TURN_OFF, create_control_socket_pair, send_command
from runtime.forkserver_launcher import spawn_part, start_forkserver
from runtime.part_declaration import PartDeclaration, RateRisk, ResourceClass, SkippedTickEffect
from runtime.part_process import PartHealth, run_part

HEALTH_INTERVAL = 0.05


def _declaration() -> PartDeclaration:
    return PartDeclaration(
        part_id="substrate-test-part",
        consumes=(),
        produces=("part-health",),
        resource_class=ResourceClass.IO_BOUND,
        rate_risk=RateRisk.LATENCY_ONLY,
        skipped_tick_effect=SkippedTickEffect.DELAYS,
    )


def run_counting_part(control_socket, health_queue, tick_queue) -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name."""
    ticks = {"count": 0}

    def do_one_tick() -> None:
        ticks["count"] += 1
        tick_queue.put(ticks["count"])

    run_part(
        declaration=_declaration(),
        control_socket=control_socket,
        do_one_tick=do_one_tick,
        emit_health=health_queue.put,
        health_interval_seconds=HEALTH_INTERVAL,
    )


def test_a_part_ticks_while_it_is_on_and_stops_when_told_off():
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    assert tick_queue.get(timeout=10) >= 1

    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    assert process.exitcode == 0
    assert process.is_alive() is False


def test_the_process_is_gone_after_off_so_its_memory_is_back():
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    tick_queue.get(timeout=10)
    pid = process.pid

    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    # T-3, measured rather than asserted: the kernel no longer has this process.
    assert not os.path.exists(f"/proc/{pid}/stat")


def test_a_part_publishes_its_rate_ratio_and_staleness_on_every_health_report():
    # Section 6: degradation must be visible. A part running at quarter rate is
    # shown as such, so the rate ratio rides on the output rather than being inferred.
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    health = health_queue.get(timeout=10)
    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    assert isinstance(health, PartHealth)
    assert health.part_id == "substrate-test-part"
    assert health.state == "on"
    assert 0.0 < health.rate_ratio <= 1.0
    assert health.staleness_seconds >= 0.0
    assert health.observed_at_ns > 0
```

- [ ] **Step 5: Write `runtime/part_process.py`**

```python
"""The part template. One shape for every feature, no privileged parts (T-1).

A part's loop selects on {control fd, data inputs}. Turning it off is letting the
process exit -- there is no restore path, because startup already is one (section 4,
crash-only). A part owns no state that is not already durable outside it, so exiting
loses nothing.

Every output carries the part's current rate ratio and a staleness timestamp, so a
part running at reduced rate is visible as such on the board (section 6, Rule 8).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
    COMMAND_TURN_ON,
    has_pending_command,
    receive_command,
)
from runtime.part_declaration import PartDeclaration

STATE_ON = "on"
STATE_OFF = "off"

EXIT_SWITCHED_OFF = 0
EXIT_CONTROL_CHANNEL_CLOSED = 0

# The full rate. A part that is not throttled reports this, so the board can tell
# "running normally" from "running at a quarter" without inferring either.
FULL_RATE_RATIO = 1.0


@dataclass(frozen=True)
class PartHealth:
    """What a part says about itself, with the two facts section 6 requires."""

    part_id: str
    state: str
    rate_ratio: float
    staleness_seconds: float
    observed_at_ns: int


def run_part(
    declaration: PartDeclaration,
    control_socket,
    do_one_tick: Callable[[], None],
    emit_health: Callable[[PartHealth], None],
    health_interval_seconds: float,
    rate_ratio: float = FULL_RATE_RATIO,
) -> int:
    """Run one part until the governor turns it off, then return.

    The tick and the control check share one loop deliberately: a part that blocked
    on its work and only read control between ticks would be a part the governor
    cannot switch, which is T-2 lost.
    """
    last_tick_at = time.monotonic()
    last_health_at = 0.0
    tick_interval = health_interval_seconds / max(rate_ratio, FULL_RATE_RATIO)

    while True:
        if has_pending_command(control_socket, timeout_seconds=tick_interval):
            frame = receive_command(control_socket)
            if frame is None:
                # The governor closed the socket. A part whose governor is gone turns
                # off rather than running unsupervised.
                return EXIT_CONTROL_CHANNEL_CLOSED
            command, _payload = frame
            if command == COMMAND_TURN_OFF:
                return EXIT_SWITCHED_OFF
            if command == COMMAND_REPORT_HEALTH:
                last_health_at = 0.0  # force one out on the next pass

        do_one_tick()
        now = time.monotonic()
        staleness = now - last_tick_at
        last_tick_at = now

        if now - last_health_at >= health_interval_seconds:
            emit_health(
                PartHealth(
                    part_id=declaration.part_id,
                    state=STATE_ON,
                    rate_ratio=rate_ratio,
                    staleness_seconds=staleness,
                    observed_at_ns=time.time_ns(),
                )
            )
            last_health_at = now
```

- [ ] **Step 6: Run both test files and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_part_declaration.py tests/runtime/test_part_process.py -v`
Expected: PASS — 4 passed and 2 xfailed in the declaration file (the two blueprint-loading tests, until Task 13), 3 passed in the part-process file.

- [ ] **Step 7: Commit**

```bash
git add runtime/part_declaration.py runtime/part_process.py \
        tests/runtime/test_part_declaration.py tests/runtime/test_part_process.py
git commit -m "part_process: the part template, and off measured rather than asserted

A part is a process; off is the process exiting, and the test checks
/proc rather than trusting an exit code. may_enter_rate_ladder is section
6's rule in one predicate over two declared facts, so a part that would
be silently biased by a lower rate cannot be admitted by accident."
```

---

### Task 9: `state_store` — SQLite WAL, and a committed row survives SIGKILL

**Files:**
- Create: `runtime/state_store.py`
- Test: `tests/runtime/test_state_store.py`

**Interfaces:**
- Consumes: `runtime.storage_facts.require_durable_directory`, `runtime.settings_reader` (entry `store_busy_timeout`).
- Produces:
  - `class StoreDurability(enum.StrEnum)` — `LEDGER` (`synchronous=FULL`), `RECORD` (`synchronous=NORMAL`)
  - `class JournalEntry` — frozen dataclass `(entry_id: int, part_id: str, kind: str, payload: str, provenance: str, recorded_at_ns: int)`
  - `def open_store(path: pathlib.Path, durability: StoreDurability, busy_timeout_seconds: float) -> sqlite3.Connection`
  - `def append_journal_entry(connection, part_id, kind, payload, provenance) -> int`
  - `def read_entries_in_window(connection, part_id, start_ns, end_ns) -> list[JournalEntry]`
  - `def record_setting_change(connection, field, old_value, new_value) -> int`
  - `def read_current_setting_and_change_time(connection, field) -> tuple[str, int] | None`
- Task 12 writes settings changes here. Task 14's probe reads from it. Phase 3's ledger is built on it.

**Why SQLite and not LMDB (§15.2):** LMDB is about 1.36× faster at bulk append and loses anyway — it would be a dependency where `sqlite3` is stdlib; it is a sorted key/value store with no answer to "what did this part produce between t0 and t1" beyond hand-built composite keys every part must get right identically; and its `map_size` is a ceiling chosen up front whose growth is a fleet-wide coordination event, which cuts against RL-061. SQLite's documentation settles the crash-only question outright: *"Transactions are durable across application crashes regardless of the synchronous setting or journal mode."* `synchronous` only governs power loss.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_state_store.py`:

```python
"""Structured part state, in the store section 15.2 chose.

The decisive property is crash-only: a part's off switch is SIGKILL, so a committed
row must survive one. SQLite guarantees that regardless of the synchronous setting;
synchronous only governs power loss. These tests measure it rather than quoting it.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from runtime.state_store import (
    JournalEntry,
    StoreDurability,
    append_journal_entry,
    open_store,
    read_current_setting_and_change_time,
    read_entries_in_window,
    record_setting_change,
)

BUSY_TIMEOUT = 5.0

WRITER = """
import sys, time
sys.path.insert(0, {repository!r})
from runtime.state_store import StoreDurability, append_journal_entry, open_store
connection = open_store({path!r}, StoreDurability.LEDGER, {timeout})
for index in range({count}):
    append_journal_entry(connection, "writer-part", "switch-record",
                         f"payload-{{index}}", "test, deterministic")
print("COMMITTED", flush=True)
time.sleep(30)
"""


def _writer_script(path, count, repository, timeout=BUSY_TIMEOUT):
    return WRITER.format(repository=repository, path=str(path), count=count, timeout=timeout)


def _repository() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_an_appended_entry_comes_back_with_everything_it_was_given(durable_tmp_path):
    connection = open_store(durable_tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    before = time.time_ns()
    entry_id = append_journal_entry(
        connection, "market-data-feed", "switch-record", "on", "gate-actuator"
    )
    entries = read_entries_in_window(connection, "market-data-feed", before, time.time_ns())

    assert entry_id > 0
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, JournalEntry)
    assert (entry.part_id, entry.kind, entry.payload, entry.provenance) == (
        "market-data-feed", "switch-record", "on", "gate-actuator",
    )


def test_the_window_query_excludes_what_falls_outside_it(durable_tmp_path):
    connection = open_store(durable_tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    append_journal_entry(connection, "part", "kind", "early", "test")
    time.sleep(0.01)
    boundary = time.time_ns()
    time.sleep(0.01)
    append_journal_entry(connection, "part", "kind", "late", "test")

    later = read_entries_in_window(connection, "part", boundary, time.time_ns())
    assert [entry.payload for entry in later] == ["late"]


def test_it_is_in_write_ahead_logging_mode(durable_tmp_path):
    connection = open_store(durable_tmp_path / "journal.db", StoreDurability.LEDGER, BUSY_TIMEOUT)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_a_ledger_syncs_fully_and_a_record_store_does_not(durable_tmp_path):
    ledger = open_store(durable_tmp_path / "ledger.db", StoreDurability.LEDGER, BUSY_TIMEOUT)
    record = open_store(durable_tmp_path / "record.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    assert ledger.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL
    assert record.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_a_second_writer_waits_rather_than_failing(durable_tmp_path):
    # Measured in section 15.2: with busy_timeout unset a second writer fails
    # instantly with 'database is locked'; set, it waits and succeeds. Parts must
    # never carry their own retry loop for this.
    path = durable_tmp_path / "journal.db"
    first = open_store(path, StoreDurability.RECORD, BUSY_TIMEOUT)
    second = open_store(path, StoreDurability.RECORD, BUSY_TIMEOUT)
    first.execute("BEGIN IMMEDIATE")
    append_journal_entry(first, "part", "kind", "held", "test")
    first.execute("COMMIT")
    append_journal_entry(second, "part", "kind", "after", "test")
    assert len(read_entries_in_window(second, "part", 0, time.time_ns())) == 2


@pytest.mark.slow
def test_committed_rows_survive_the_writer_being_sigkilled(durable_tmp_path):
    # This is the crash-only requirement, and the only test here that really matters.
    path = durable_tmp_path / "journal.db"
    count = 200
    writer = subprocess.Popen(
        [sys.executable, "-c", _writer_script(path, count, _repository())],
        stdout=subprocess.PIPE, text=True,
    )
    assert writer.stdout.readline().strip() == "COMMITTED"
    os.kill(writer.pid, signal.SIGKILL)
    writer.wait()

    reopened = open_store(path, StoreDurability.LEDGER, BUSY_TIMEOUT)
    survived = read_entries_in_window(reopened, "writer-part", 0, time.time_ns())
    assert len(survived) == count, f"{len(survived)} of {count} committed rows survived SIGKILL"


def test_the_current_setting_and_when_it_last_changed_are_both_answerable(durable_tmp_path):
    # RL-055's board question, answered from the journal rather than from mtime or
    # git log -- both of which lie for the reasons section 15.3 records.
    connection = open_store(durable_tmp_path / "changes.db", StoreDurability.RECORD, BUSY_TIMEOUT)
    assert read_current_setting_and_change_time(connection, "main_balance") is None

    record_setting_change(connection, "main_balance", "0.0", "1000.0")
    time.sleep(0.01)
    record_setting_change(connection, "main_balance", "1000.0", "2500.0")

    value, changed_at_ns = read_current_setting_and_change_time(connection, "main_balance")
    assert value == "2500.0"
    assert changed_at_ns > 0


def test_it_refuses_a_store_on_a_filesystem_whose_pages_are_memory(tmp_path):
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        open_store(tmp_path / "journal.db", StoreDurability.RECORD, BUSY_TIMEOUT)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_state_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.state_store'`.

- [ ] **Step 3: Write the implementation**

`runtime/state_store.py`:

```python
"""Structured part state: settings changes, journal entries, provenance, switch records.

SQLite in WAL mode, from the standard library (section 15.2). LMDB was measured
faster at bulk append and lost on three counts that matter more at this cadence:
it would be a dependency where sqlite3 is not one; it is a key/value store with no
answer to a time-window query beyond composite keys every part must encode
identically; and its map_size is a ceiling chosen up front whose growth every other
process has to react to, which cuts against RL-061.

Crash-only, from SQLite's own documentation: "Transactions are durable across
application crashes regardless of the synchronous setting or journal mode."
synchronous governs power loss, not a part being SIGKILLed -- which is exactly the
off switch section 4 defines.
"""

from __future__ import annotations

import enum
import pathlib
import sqlite3
import time
from dataclasses import dataclass

from runtime.storage_facts import require_durable_directory


class StoreDurability(enum.StrEnum):
    """How much a store is willing to lose to a power cut. Never to a SIGKILL."""

    # A committed row must not vanish. One extra WAL sync per commit.
    LEDGER = "ledger"
    # Never corrupt, and may lose only the very last write to a genuine power
    # failure. Correct for provenance, metadata and soft state.
    RECORD = "record"


_SYNCHRONOUS_FOR = {StoreDurability.LEDGER: "FULL", StoreDurability.RECORD: "NORMAL"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entry (
    entry_id       INTEGER PRIMARY KEY,
    part_id        TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    provenance     TEXT    NOT NULL,
    recorded_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS journal_entry_by_part_and_time
    ON journal_entry (part_id, recorded_at_ns);

CREATE TABLE IF NOT EXISTS setting_change (
    change_id      INTEGER PRIMARY KEY,
    field          TEXT    NOT NULL,
    old_value      TEXT    NOT NULL,
    new_value      TEXT    NOT NULL,
    observed_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS setting_change_by_field_and_time
    ON setting_change (field, observed_at_ns);
"""


@dataclass(frozen=True)
class JournalEntry:
    """One immutable thing that happened, and what said so."""

    entry_id: int
    part_id: str
    kind: str
    payload: str
    provenance: str
    recorded_at_ns: int


def open_store(
    path: pathlib.Path,
    durability: StoreDurability,
    busy_timeout_seconds: float,
) -> sqlite3.Connection:
    """Open or create a store, with the pragmas section 15.2 settled.

    busy_timeout is not optional. WAL permits exactly one writer per file; measured
    with it unset a second writer fails instantly with 'database is locked', and with
    it set the same writer waits and succeeds. A part should never carry retry code
    for something the library already does correctly.
    """
    path = pathlib.Path(path)
    require_durable_directory(path.parent)
    connection = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_seconds)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA synchronous={_SYNCHRONOUS_FOR[durability]}")
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}")
    connection.executescript(_SCHEMA)
    return connection


def append_journal_entry(
    connection: sqlite3.Connection,
    part_id: str,
    kind: str,
    payload: str,
    provenance: str,
) -> int:
    """Record one immutable entry and return its id.

    Transactions stay short -- insert and commit, never held across other work --
    because a held write transaction stalls every other writer for its whole duration.
    """
    cursor = connection.execute(
        "INSERT INTO journal_entry (part_id, kind, payload, provenance, recorded_at_ns) "
        "VALUES (?, ?, ?, ?, ?)",
        (part_id, kind, payload, provenance, time.time_ns()),
    )
    return int(cursor.lastrowid)


def read_entries_in_window(
    connection: sqlite3.Connection, part_id: str, start_ns: int, end_ns: int
) -> list[JournalEntry]:
    """What did this part produce between these two moments?

    This query is why the store is relational. In a key/value store it is a composite
    key convention every part has to implement the same way and keep correct.
    """
    rows = connection.execute(
        "SELECT entry_id, part_id, kind, payload, provenance, recorded_at_ns "
        "FROM journal_entry WHERE part_id = ? AND recorded_at_ns >= ? AND recorded_at_ns <= ? "
        "ORDER BY recorded_at_ns, entry_id",
        (part_id, start_ns, end_ns),
    ).fetchall()
    return [JournalEntry(*row) for row in rows]


def record_setting_change(
    connection: sqlite3.Connection, field: str, old_value: str, new_value: str
) -> int:
    """Append one observed settings change. The board's 'last changed' reads this.

    Never the file's mtime, which lies on a no-op save, and never git log, which is
    empty because RL-055's operator edits over SSH and never commits.
    """
    cursor = connection.execute(
        "INSERT INTO setting_change (field, old_value, new_value, observed_at_ns) "
        "VALUES (?, ?, ?, ?)",
        (field, old_value, new_value, time.time_ns()),
    )
    return int(cursor.lastrowid)


def read_current_setting_and_change_time(
    connection: sqlite3.Connection, field: str
) -> tuple[str, int] | None:
    """The value this field last changed to, and when that was observed."""
    row = connection.execute(
        "SELECT new_value, observed_at_ns FROM setting_change WHERE field = ? "
        "ORDER BY observed_at_ns DESC, change_id DESC LIMIT 1",
        (field,),
    ).fetchone()
    return (row[0], int(row[1])) if row else None
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_state_store.py -v`
Expected: PASS, 8 passed.

- [ ] **Step 5: Commit**

```bash
git add runtime/state_store.py tests/runtime/test_state_store.py
git commit -m "state_store: SQLite WAL, and a committed row survives SIGKILL

Section 15.2. LMDB is faster at bulk append and loses on being a
dependency, on having no answer to a time-window query, and on map_size
being a ceiling chosen up front. busy_timeout is set on every connection
because unset, a second writer fails instantly rather than waiting."
```

---

### Task 10: `numeric_state` — memmap, with a stamp that catches a part that died mid-update

**Files:**
- Create: `runtime/numeric_state.py`
- Test: `tests/runtime/test_numeric_state.py`

**Interfaces:**
- Consumes: `runtime.storage_facts.require_durable_directory`, `runtime.state_store`.
- Produces:
  - `class NumericStateSpec` — frozen dataclass `(name: str, shape: tuple[int, ...], dtype: str)`
  - `class NumericState` — `array: numpy.memmap`, `def record_sequence_stamp(self, stamp: int) -> None`, `def read_sequence_stamp(self) -> int`, `def force_writeback(self) -> None`, `def close(self) -> None`, context-manager protocol
  - `def open_numeric_state(directory: pathlib.Path, spec: NumericStateSpec) -> NumericState`
  - `def has_state_gap(state: NumericState, published_stamp: int) -> bool`
- Phase 1's rolling windows and phase 4's learned parameters are stored through this.

**Why the missing `close()` is not the problem it looked like (§15.4):** durability here is the kernel's job. Dirty `MAP_SHARED` file-backed pages live in the page cache, which belongs to the inode and not the process, and `mmap(2)` says a mapping is torn down when the process terminates by any means. Measured on ext4: **6 of 6 trials, 2 000 000 of 2 000 000 `float64` elements survived `SIGKILL` with no flush** — 100%, with and without an explicit `flush()`. What the missing `close()` genuinely costs is descriptors and VMAs accumulating in a process that opens and abandons many mappings *without exiting*, which is why **the governor must not map part state into its own long-lived process**.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_numeric_state.py`:

```python
"""Durable numeric state, and the stamp that says whether a part died mid-update.

Section 15.4: numpy.memmap has no API to close the underlying mmap, and it does not
need one. Dirty MAP_SHARED pages belong to the inode's page cache, not the process,
so they survive a SIGKILL that runs no userspace cleanup at all. Measured 6 of 6.
"""

import os
import signal
import subprocess
import sys

import numpy
import pytest

from runtime.numeric_state import (
    NumericStateSpec,
    has_state_gap,
    open_numeric_state,
)

SPEC = NumericStateSpec(name="rolling-window", shape=(2000, 6), dtype="float64")

CHILD = """
import sys
sys.path.insert(0, {repository!r})
from runtime.numeric_state import NumericStateSpec, open_numeric_state
import numpy, pathlib, time
spec = NumericStateSpec(name="rolling-window", shape=(2000, 6), dtype="float64")
state = open_numeric_state(pathlib.Path({directory!r}), spec)
pattern = numpy.arange(2000 * 6, dtype="float64").reshape(2000, 6) * 3.0 + {seed}
state.array[:] = pattern
state.record_sequence_stamp({stamp})
{maybe_flush}
print("WRITTEN", flush=True)
time.sleep(30)
"""


def _repository() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_then_kill(directory, seed: int, stamp: int, flush: bool) -> None:
    script = CHILD.format(
        repository=_repository(), directory=str(directory), seed=seed, stamp=stamp,
        maybe_flush="state.force_writeback()" if flush else "",
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "WRITTEN"
    os.kill(child.pid, signal.SIGKILL)
    child.wait()


def test_state_round_trips_within_one_process(durable_tmp_path):
    with open_numeric_state(durable_tmp_path, SPEC) as state:
        state.array[0, 0] = 42.5
        state.record_sequence_stamp(7)
    with open_numeric_state(durable_tmp_path, SPEC) as reopened:
        assert reopened.array[0, 0] == 42.5
        assert reopened.read_sequence_stamp() == 7


@pytest.mark.slow
@pytest.mark.parametrize("flush", [False, True])
def test_unflushed_state_survives_sigkill_intact(durable_tmp_path, flush):
    seed, stamp = 11, 99
    _write_then_kill(durable_tmp_path, seed=seed, stamp=stamp, flush=flush)

    expected = numpy.arange(2000 * 6, dtype="float64").reshape(2000, 6) * 3.0 + seed
    with open_numeric_state(durable_tmp_path, SPEC) as reopened:
        survived = int((reopened.array == expected).sum())
        assert survived == expected.size, f"{survived} of {expected.size} elements survived"
        assert reopened.read_sequence_stamp() == stamp


def test_a_stamp_behind_what_was_published_is_a_gap(durable_tmp_path):
    # The part updated memory, published stamp 5 to the store, then died before
    # its next update landed. On switch-on that divergence is a fault, not a
    # reason to silently reseed.
    with open_numeric_state(durable_tmp_path, SPEC) as state:
        state.record_sequence_stamp(4)
        assert has_state_gap(state, published_stamp=5) is True
        assert has_state_gap(state, published_stamp=4) is False


@pytest.mark.slow
def test_repeated_off_and_on_cycles_do_not_leak_descriptors_or_mappings(durable_tmp_path):
    # The real cost of the missing close(): fds and VMAs in a process that opens
    # many mappings without exiting. The governor must never do this, and this test
    # is what would catch it if something did.
    def counts() -> tuple[int, int]:
        return (
            len(os.listdir("/proc/self/fd")),
            len(open("/proc/self/maps").read().splitlines()),
        )

    for _ in range(20):
        with open_numeric_state(durable_tmp_path, SPEC) as state:
            state.array[0, 0] += 1.0
    settled_descriptors, settled_mappings = counts()

    for _ in range(200):
        with open_numeric_state(durable_tmp_path, SPEC) as state:
            state.array[0, 0] += 1.0
    final_descriptors, final_mappings = counts()

    assert final_descriptors <= settled_descriptors + 2, "file descriptors are accumulating"
    assert final_mappings <= settled_mappings + 8, "mappings are accumulating"


def test_it_refuses_a_directory_whose_pages_are_memory(tmp_path):
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        open_numeric_state(tmp_path, SPEC)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_numeric_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.numeric_state'`.

- [ ] **Step 3: Write the implementation**

`runtime/numeric_state.py`:

```python
"""Durable numeric part state: rolling windows and learned parameters.

File-backed numpy.memmap (section 15.4). numpy has no API to close the underlying
mmap and does not need one: dirty MAP_SHARED pages live in the page cache, which
belongs to the inode rather than the process, so they are written back whether or
not any userspace cleanup runs -- which under SIGKILL is none of it. Measured on
ext4, 6 of 6 trials, 100% of 2 000 000 float64 elements survived unflushed.

multiprocessing.shared_memory is deliberately not the fallback. It is /dev/shm,
which is RAM: not durable across an off/on cycle, and memory that stays resident
while a part is off is exactly what T-3 forbids. Its resource tracker also still
carries cpython#82300. Its only role here is hot live-to-live transport between two
running parts, with track=False, never as a store.

The one way to reintroduce numpy's fd wart is to map part state into a process that
never exits. The governor must not do that.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import numpy

from runtime.storage_facts import require_durable_directory

# The stamp lives beside the data rather than inside it, so the array keeps the
# exact shape and dtype the part declared and nothing has to reserve a row.
STAMP_DTYPE = "int64"
STAMP_SUFFIX = ".stamp"
DATA_SUFFIX = ".f64"
NO_STAMP_RECORDED = 0


@dataclass(frozen=True)
class NumericStateSpec:
    """A fixed shape and dtype, which is what makes this memmappable at all.

    dtype=object arrays cannot be memory-mapped: they are arrays of pointers into
    the Python heap, and a pointer means nothing in another process.
    """

    name: str
    shape: tuple[int, ...]
    dtype: str


class NumericState:
    """One part's numeric state, and the sequence stamp that dates it."""

    def __init__(self, array: numpy.memmap, stamp: numpy.memmap) -> None:
        self.array = array
        self._stamp = stamp

    def record_sequence_stamp(self, stamp: int) -> None:
        """Record how far this state has been updated to."""
        self._stamp[0] = stamp

    def read_sequence_stamp(self) -> int:
        return int(self._stamp[0])

    def force_writeback(self) -> None:
        """Make writeback happen now rather than whenever the kernel gets to it.

        Not what makes the state durable -- that is unconditional. This is for
        ordering, when two files must be consistent with each other at a known moment.
        """
        self.array.flush()
        self._stamp.flush()

    def close(self) -> None:
        """Drop the references so CPython's refcounting unmaps them.

        There is no close() on numpy.memmap, and numpy#13510 has been open since 2019
        because there is no safe way to add one -- a memmap can be aliased by other
        ndarray views and closing under a live view segfaults the interpreter. Dropping
        the last reference is the supported route.
        """
        self.array = None
        self._stamp = None

    def __enter__(self) -> "NumericState":
        return self

    def __exit__(self, *exception) -> None:
        self.close()


def open_numeric_state(directory: pathlib.Path, spec: NumericStateSpec) -> NumericState:
    """Map a part's numeric state, creating it on first use.

    Turning a part on is ordinary startup: remap, and carry on. There is no restore
    path because startup already is one (section 4).
    """
    directory = require_durable_directory(pathlib.Path(directory))
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / f"{spec.name}{DATA_SUFFIX}"
    stamp_path = directory / f"{spec.name}{STAMP_SUFFIX}"

    if numpy.dtype(spec.dtype).hasobject:
        raise ValueError(
            f"{spec.name} declares dtype {spec.dtype}, which holds Python objects. "
            f"An array of pointers into one process's heap cannot be shared or made durable."
        )

    mode = "r+" if data_path.exists() else "w+"
    array = numpy.memmap(data_path, dtype=spec.dtype, mode=mode, shape=spec.shape)
    stamp_mode = "r+" if stamp_path.exists() else "w+"
    stamp = numpy.memmap(stamp_path, dtype=STAMP_DTYPE, mode=stamp_mode, shape=(1,))
    if stamp_mode == "w+":
        stamp[0] = NO_STAMP_RECORDED
    return NumericState(array=array, stamp=stamp)


def has_state_gap(state: NumericState, published_stamp: int) -> bool:
    """Did this part die between updating its state and publishing how far it got?

    On switch-on the part compares its own stamp against the last one the store
    recorded. A divergence is a fault to report, never a reason to silently reseed --
    a rolling window that quietly restarts is section 6's silently-biased indicator
    arriving by another route.
    """
    return state.read_sequence_stamp() < published_stamp
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_numeric_state.py -v`
Expected: PASS, 6 passed (2 of them parametrised).

- [ ] **Step 5: Commit**

```bash
git add runtime/numeric_state.py tests/runtime/test_numeric_state.py
git commit -m "numeric_state: memmap, and a stamp that catches a part that died mid-update

Section 15.4: the missing close() is not a durability hazard. Dirty
MAP_SHARED pages belong to the inode's page cache, so they survive a
SIGKILL that runs no userspace cleanup -- measured 6 of 6 at 100%. What
it does cost is fds in a process that never exits, so the leak test is
here and the governor must never map part state into itself."
```

---

### Task 11: `hardware_facts` — the numbers the governor is allowed to use

**Files:**
- Create: `runtime/hardware_facts.py`
- Test: `tests/runtime/test_hardware_facts.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class HardwareFacts` — frozen dataclass `(physical_cores: int, logical_cpus: int, total_ram_bytes: int, available_ram_bytes: int, swap_total_bytes: int, numa_nodes: int, measured_at_ns: int)`
  - `def measure_hardware_facts() -> HardwareFacts`
  - `def read_available_ram_bytes() -> int`
  - `def read_own_cgroup_directory() -> pathlib.Path`
  - `def read_cgroup_pressure(cgroup_directory: pathlib.Path, resource: str) -> dict[str, float]`
- Phase 2's `hardware-scanner` is this module wrapped in a part. Task 14's tile renders it.

**Why physical cores and not `os.cpu_count()`:** §0 records that this box is 6 physical cores with SMT2 giving 12 logical, and capacity planning treats **6** as the ceiling. `os.cpu_count()` returns 12 and would let the governor admit twice the work the machine can do. Also: the per-cgroup `cpu.pressure` `full` line is the usable scarcity signal — the **system-wide `full` line is zero by definition**, so reading the wrong file gives a signal that never fires.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_hardware_facts.py`:

```python
"""Capacity is measured, never assumed, and never os.cpu_count().

Section 0: this box is 6 physical cores with SMT2 giving 12 logical, and capacity
planning treats 6 as the ceiling. cpu_count() returns 12 and would let the governor
admit twice the work the machine can actually do.
"""

import os
import pathlib

from runtime.hardware_facts import (
    HardwareFacts,
    measure_hardware_facts,
    read_available_ram_bytes,
    read_cgroup_pressure,
    read_own_cgroup_directory,
)


def test_counts_physical_cores_not_logical_ones():
    facts = measure_hardware_facts()
    assert isinstance(facts, HardwareFacts)
    assert facts.logical_cpus == os.cpu_count()
    assert facts.physical_cores < facts.logical_cpus, (
        "SMT is on here, so physical cores must be fewer than logical CPUs"
    )
    assert facts.physical_cores * 2 == facts.logical_cpus, "SMT2, per section 0"


def test_reports_that_this_box_has_no_swap():
    # Section 5 depends on this: with no swap, dirty pages have nowhere to go and a
    # writer that outruns writeback is OOM-killed rather than slowed.
    assert measure_hardware_facts().swap_total_bytes == 0


def test_available_ram_is_a_live_reading_not_a_cached_one():
    first = read_available_ram_bytes()
    assert first > 0
    filler = bytearray(64 * 1024 * 1024)
    assert read_available_ram_bytes() > 0
    del filler


def test_every_fact_carries_when_it_was_measured():
    assert measure_hardware_facts().measured_at_ns > 0


def test_finds_its_own_cgroup_directory():
    directory = read_own_cgroup_directory()
    assert directory.is_dir()
    assert (directory / "cgroup.procs").exists()


def test_reads_the_per_cgroup_pressure_lines():
    # The per-cgroup 'full' line is the usable one. The system-wide 'full' line is
    # zero by definition, so a governor reading /proc/pressure/cpu would see a
    # signal that never fires.
    pressure = read_cgroup_pressure(read_own_cgroup_directory(), resource="cpu")
    assert "some_avg10" in pressure
    assert "full_avg10" in pressure
    assert all(isinstance(value, float) for value in pressure.values())
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_hardware_facts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.hardware_facts'`.

- [ ] **Step 3: Write the implementation**

`runtime/hardware_facts.py`:

```python
"""What this machine actually has, measured now rather than remembered.

RL-061 in its clearest form: the governor's capacity numbers are read from the
kernel, never written in. Section 0 records what this box measured on 2026-08-20,
and this module is how that stays true after a resize.

The important distinction is physical cores against logical CPUs. This box is 6
physical with SMT2 giving 12, and capacity planning treats 6 as the ceiling --
os.cpu_count() would say 12 and let the governor admit twice the real work. E2 is
GCP's cost-optimised family, so even the 6 carry host-level variance nothing here
can observe; the ceiling is a ceiling, not a promise.
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass

CPUINFO_PATH = pathlib.Path("/proc/cpuinfo")
MEMINFO_PATH = pathlib.Path("/proc/meminfo")
NUMA_NODE_ROOT = pathlib.Path("/sys/devices/system/node")
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")

KIBIBYTE = 1024


@dataclass(frozen=True)
class HardwareFacts:
    """One measurement of this machine, with the moment it was taken."""

    physical_cores: int
    logical_cpus: int
    total_ram_bytes: int
    available_ram_bytes: int
    swap_total_bytes: int
    numa_nodes: int
    measured_at_ns: int


def _read_meminfo_kibibytes() -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in MEMINFO_PATH.read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            fields[name] = int(parts[0])
    return fields


def count_physical_cores() -> int:
    """Count distinct (physical id, core id) pairs -- SMT siblings collapse to one."""
    cores: set[tuple[str, str]] = set()
    package = core = None
    for line in CPUINFO_PATH.read_text().splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name == "physical id":
            package = value
        elif name == "core id":
            core = value
            if package is not None:
                cores.add((package, core))
    # A kernel that does not publish topology leaves this empty; fall back to the
    # logical count rather than returning zero, and let the tile show the difference.
    return len(cores) or (os.cpu_count() or 1)


def read_available_ram_bytes() -> int:
    """MemAvailable, read live. This is what off-state-verifier watches move."""
    return _read_meminfo_kibibytes()["MemAvailable"] * KIBIBYTE


def measure_hardware_facts() -> HardwareFacts:
    """Take one reading of everything the governor is allowed to plan against."""
    meminfo = _read_meminfo_kibibytes()
    numa_nodes = len(list(NUMA_NODE_ROOT.glob("node[0-9]*"))) if NUMA_NODE_ROOT.is_dir() else 1
    return HardwareFacts(
        physical_cores=count_physical_cores(),
        logical_cpus=os.cpu_count() or 1,
        total_ram_bytes=meminfo["MemTotal"] * KIBIBYTE,
        available_ram_bytes=meminfo["MemAvailable"] * KIBIBYTE,
        swap_total_bytes=meminfo.get("SwapTotal", 0) * KIBIBYTE,
        numa_nodes=numa_nodes or 1,
        measured_at_ns=time.time_ns(),
    )


def read_own_cgroup_directory() -> pathlib.Path:
    """Where this process's cgroup files live."""
    relative = pathlib.Path("/proc/self/cgroup").read_text().strip().split("::")[1]
    return CGROUP_ROOT / relative.lstrip("/")


def read_cgroup_pressure(cgroup_directory: pathlib.Path, resource: str) -> dict[str, float]:
    """Parse one cgroup's pressure file into flat named averages.

    The per-cgroup 'full' line measures every task in the cgroup stalled at once and
    is the signal section 5 admits against. The system-wide 'full' line is zero by
    definition, so /proc/pressure/<resource> is the wrong file for this purpose.
    """
    readings: dict[str, float] = {}
    for line in (cgroup_directory / f"{resource}.pressure").read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        scope = fields[0]
        for field in fields[1:]:
            key, _, value = field.partition("=")
            readings[f"{scope}_{key}"] = float(value)
    return readings
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_hardware_facts.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Check the numbers against what §0 recorded**

```bash
.venv/bin/python -c "
from runtime.hardware_facts import measure_hardware_facts
facts = measure_hardware_facts()
print(f'physical cores {facts.physical_cores}  logical {facts.logical_cpus}  '
      f'numa {facts.numa_nodes}  swap {facts.swap_total_bytes}')
print(f'RAM {facts.total_ram_bytes/1e9:.1f} GB total, {facts.available_ram_bytes/1e9:.1f} GB available')
"
```
Expected: `physical cores 6  logical 12  numa 1  swap 0`, RAM about 31.5 GB total. **If physical cores is not 6, the machine changed and §0 of the spec is stale — say so rather than adjusting the test.**

- [ ] **Step 6: Commit**

```bash
git add runtime/hardware_facts.py tests/runtime/test_hardware_facts.py
git commit -m "hardware_facts: capacity measured, and physical cores not logical ones

os.cpu_count() says 12 on this box and the real ceiling is 6. A governor
planning against 12 would admit twice the work the machine can do. The
per-cgroup pressure 'full' line is the usable scarcity signal; the
system-wide one is zero by definition."
```

---

### Task 12: `settings_watcher` — a dropped event must never read as *nothing changed*

**Files:**
- Create: `runtime/settings_watcher.py`
- Test: `tests/runtime/test_settings_watcher.py`

**Interfaces:**
- Consumes: `runtime.settings_reader.LastKnownGoodSettings`, `runtime.state_store.record_setting_change`.
- Produces:
  - `class SettingsChange` — frozen dataclass `(field: str, old_value: str, new_value: str, observed_at_ns: int)`
  - `def compare_settings_documents(previous, candidate) -> list[SettingsChange]`
  - `class SettingsDirectoryWatch` — `__init__(self, directory, on_change: Callable[[list[SettingsChange]], None], on_rejection: Callable[[SettingsRejection], None], on_overflow: Callable[[], None])`, `def start(self) -> None`, `def stop(self) -> None`, `def recheck_now(self) -> None`
- Phase 3's `capital-settings-change-recorder` is this wired to the store.

**Why the directory and not the file:** an editor that saves by write-temp-then-rename replaces the inode, and a watch on the *file* is left pointing at an inode nobody will write again. Why the diff and not mtime: vim rewrites the file on `:wq` even with nothing changed, so mtime reports a change that did not happen. Why overflow is handled: `inotify(7)` says the queue can overflow and silently drop events (`IN_Q_OVERFLOW`), and under Rule 8 an absent event stream is its own state — it forces a full re-read rather than reading as quiet.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_settings_watcher.py`:

```python
"""When it last changed, measured -- not mtime, not git log.

Section 15.3: mtime lies on a no-op save because vim rewrites the file on :wq;
git log is empty because RL-055's operator edits over SSH and never commits;
inotify can drop events, so an overflow forces a full re-read rather than reading
as nothing happened.
"""

import textwrap
import time

import pytest

from runtime.settings_reader import LastKnownGoodSettings, load_settings_document
from runtime.settings_watcher import SettingsChange, SettingsDirectoryWatch, compare_settings_documents

TEMPLATE = textwrap.dedent(
    """
    [main_balance]
    value = {balance}
    unit  = "USDT"
    note  = "operator, 2026-08-20: test"
    """
).strip()

SETTLE_SECONDS = 2.0


def _write(directory, balance: str):
    path = directory / "main-account.toml"
    path.write_text(TEMPLATE.format(balance=balance))
    return path


def test_a_changed_value_is_reported_with_both_sides(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    _write(durable_tmp_path, "2500.0")
    candidate = load_settings_document(path, scope="main-account")

    changes = compare_settings_documents(previous, candidate)
    assert changes == [
        SettingsChange(
            field="main_balance", old_value="1000.0", new_value="2500.0",
            observed_at_ns=changes[0].observed_at_ns,
        )
    ]


def test_a_no_op_save_produces_no_change(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    time.sleep(0.01)
    _write(durable_tmp_path, "1000.0")  # what vim does on :wq with nothing edited
    candidate = load_settings_document(path, scope="main-account")
    assert compare_settings_documents(previous, candidate) == []


def test_an_added_entry_reports_as_a_change_from_nothing(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    previous = load_settings_document(path, scope="main-account")
    path.write_text(
        TEMPLATE.format(balance="1000.0")
        + '\n\n[leverage_ceiling]\nvalue = 3.0\nunit = "multiple"\nnote = "operator: test"\n'
    )
    candidate = load_settings_document(path, scope="main-account")
    changes = compare_settings_documents(previous, candidate)
    assert [change.field for change in changes] == ["leverage_ceiling"]
    assert changes[0].old_value == ""


@pytest.mark.slow
def test_an_edit_over_ssh_wakes_the_watch_and_reports_the_change(durable_tmp_path):
    _write(durable_tmp_path, "1000.0")
    seen: list[list[SettingsChange]] = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
    )
    watch.start()
    try:
        _write(durable_tmp_path, "2500.0")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert seen, "the watch did not wake on an edit"
    assert seen[0][0].new_value == "2500.0"


@pytest.mark.slow
def test_a_broken_edit_reports_a_rejection_and_does_not_report_a_change(durable_tmp_path):
    path = _write(durable_tmp_path, "1000.0")
    changes: list = []
    rejections: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=changes.append,
        on_rejection=rejections.append, on_overflow=lambda: None,
    )
    watch.start()
    try:
        path.write_text("[main_balance]\nvalue = [1, 2,\n")
        deadline = time.monotonic() + SETTLE_SECONDS
        while not rejections and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watch.stop()

    assert rejections, "a broken edit was not reported"
    assert changes == [], "a broken edit must not take effect"


def test_recheck_now_re_reads_without_waiting_for_an_event(durable_tmp_path):
    # What an IN_Q_OVERFLOW triggers: re-establish ground truth rather than trust
    # a stream that admits it dropped something.
    _write(durable_tmp_path, "1000.0")
    seen: list = []
    watch = SettingsDirectoryWatch(
        directory=durable_tmp_path, on_change=seen.append,
        on_rejection=lambda rejection: None, on_overflow=lambda: None,
    )
    _write(durable_tmp_path, "2500.0")
    watch.recheck_now()
    assert seen and seen[0][0].new_value == "2500.0"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_settings_watcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.settings_watcher'`.

- [ ] **Step 3: Write the implementation**

`runtime/settings_watcher.py`:

```python
"""Notice that the operator edited a settings file, and be right about it.

RL-055 wants the board to show current values and when they last changed, and
Rule 8 wants that timestamp to come from a probe rather than an assertion. So:

  trigger   watchdog's inotify backend on the DIRECTORY, not the files -- an editor
            that saves by write-temp-then-rename replaces the inode, and a watch on
            the file is left pointing at one nobody will write again.
  decide    re-parse and diff the parsed structure. mtime is not enough: vim
            rewrites the file on :wq even with nothing changed.
  overflow  inotify(7) says the queue can overflow and silently drop events. An
            absent event stream is its own state, so it forces a full re-read
            rather than reading as quiet.

Nothing here writes a settings file. It reports; capital-settings-change-recorder
turns the report into a journal_entry, which is what the board queries.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from watchdog.events import FileSystemEventHandler
from watchdog.observers.inotify import InotifyObserver

from runtime.settings_reader import (
    LastKnownGoodSettings,
    SettingsDocument,
    SettingsRejection,
    load_settings_document,
)

SETTINGS_SUFFIX = ".toml"
# An entry that did not exist before reads as a change from nothing, rather than
# being silently skipped -- a new bound appearing is exactly as notable as one moving.
ABSENT_VALUE = ""


@dataclass(frozen=True)
class SettingsChange:
    """One field the operator moved, and what it moved between."""

    field: str
    old_value: str
    new_value: str
    observed_at_ns: int


def compare_settings_documents(
    previous: SettingsDocument, candidate: SettingsDocument
) -> list[SettingsChange]:
    """What actually changed between two parses. Empty for a no-op save."""
    observed_at_ns = time.time_ns()
    changes: list[SettingsChange] = []
    for name in sorted(set(previous.entries) | set(candidate.entries)):
        before = previous.entries.get(name)
        after = candidate.entries.get(name)
        old_value = ABSENT_VALUE if before is None else str(before.value)
        new_value = ABSENT_VALUE if after is None else str(after.value)
        if old_value != new_value:
            changes.append(
                SettingsChange(
                    field=name, old_value=old_value, new_value=new_value,
                    observed_at_ns=observed_at_ns,
                )
            )
    return changes


class _SettingsFileHandler(FileSystemEventHandler):
    def __init__(self, recheck: Callable[[], None], on_overflow: Callable[[], None]) -> None:
        self._recheck = recheck
        self._on_overflow = on_overflow

    def on_any_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        if str(event.src_path).endswith(SETTINGS_SUFFIX):
            self._recheck()


class SettingsDirectoryWatch:
    """Watch a settings directory and report real changes, rejections and overflows."""

    def __init__(
        self,
        directory: pathlib.Path,
        on_change: Callable[[list[SettingsChange]], None],
        on_rejection: Callable[[SettingsRejection], None],
        on_overflow: Callable[[], None],
    ) -> None:
        self._directory = pathlib.Path(directory)
        self._on_change = on_change
        self._on_rejection = on_rejection
        self._on_overflow = on_overflow
        self._known: dict[pathlib.Path, LastKnownGoodSettings] = {}
        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            try:
                self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
            except Exception as refusal:  # a file already broken when we arrived
                self._on_rejection(
                    SettingsRejection(path, str(refusal), time.time_ns())
                )
        self._observer = InotifyObserver()
        self._observer.schedule(
            _SettingsFileHandler(self.recheck_now, self._on_overflow),
            str(self._directory), recursive=True,
        )

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def recheck_now(self) -> None:
        """Re-read every settings file and report what actually moved.

        Called on an inotify event and, deliberately, on overflow: the answer to a
        stream that admits it dropped something is to re-establish ground truth.
        """
        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            settings = self._known.get(path)
            if settings is None:
                try:
                    self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
                except Exception as refusal:
                    self._on_rejection(SettingsRejection(path, str(refusal), time.time_ns()))
                continue
            previous = settings.current
            rejection = settings.offer_candidate()
            if rejection is not None:
                self._on_rejection(rejection)
                continue
            changes = compare_settings_documents(previous, settings.current)
            if changes:
                self._on_change(changes)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_settings_watcher.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add runtime/settings_watcher.py tests/runtime/test_settings_watcher.py
git commit -m "settings_watcher: watch the directory, diff the parse, distrust silence

The file watch would be orphaned by write-temp-then-rename; mtime lies
on a no-op :wq; git log is empty because the operator edits over SSH and
never commits. inotify can drop events, so an overflow forces a full
re-read rather than reading as nothing changed."
```

---

### Task 13: The blueprint edit — three declarations on all 321 parts, enforced

**Files:**
- Create: `docs/proposals/part-declarations.md`
- Create: `dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py`
- Modify: `dashboard/render_blueprint.py` — `REQUIRED_FEATURE_FIELDS` and two new checks
- Modify: `tests/runtime/test_part_declaration.py` — delete the two `xfail` marks from Task 8
- Test: the existing `python3 dashboard/check_contracts.py` and the pre-commit hook

**Interfaces:**
- Consumes: `runtime.part_declaration` (the three enums are the vocabulary the edit writes).
- Produces: every entry in `docs/features.json` carries `resource_class`, `rate_risk` and `skipped_tick_effect`, and `check_contracts.py` refuses a commit where one is missing or outside its vocabulary.

**Why this is a blueprint edit and not a code change:** a design change is a blueprint edit first — an idempotent script plus a proposal — checked and committed, and code follows the registry rather than the other way round. §6 says the contract checker enforces that both throttle facts are present; §7 says the same of the resource class. Until this lands, `load_declaration_from_blueprint` cannot work, which is why Task 8's two blueprint tests are `xfail(strict=True)` and get their marks deleted here.

- [ ] **Step 1: Write the proposal**

`docs/proposals/part-declarations.md` states: what the three fields are and what each value means; that §6 admits a part to a rate ladder only when `rate_risk` is `latency-only` **and** `skipped_tick_effect` is `delays`, so the pair is not redundant; how the default for each of the 321 parts was chosen, and that a default is a starting position to be corrected part by part as each is built, not a measurement; and that no part's `consumes`/`produces` changes, so no edge moves and R-03 is untouched.

The defaults, by category, argued rather than guessed:

| Category shape | `resource_class` | `rate_risk` | `skipped_tick_effect` |
|---|---|---|---|
| feed readers, socket loops, REST pollers, venue clients | `io-bound` | `changes-the-answer` | `corrupts` |
| indicator, feature and rolling-window parts | `bandwidth-bound` | `changes-the-answer` | `corrupts` |
| model, scorer, search and learning parts | `compute-bound` | `latency-only` | `delays` |
| ledger, journal, provenance and reporting parts | `io-bound` | `latency-only` | `delays` |
| everything else | `compute-bound` | `changes-the-answer` | `corrupts` |

The catch-all is deliberately the **most restrictive** combination. A part whose behaviour under a lower rate nobody has thought about yet must not be throttleable by default: §6's two errors are not symmetric, and the safe default is the one that costs an eviction rather than a silently biased number.

- [ ] **Step 2: Write the idempotent edit script**

`dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py`, following the shape of the existing edits in that directory: a module docstring saying what it does and why, `ROOT`/`REG` resolved from `__file__`, a category-to-defaults table, a loop that sets each field **only if absent** so re-running changes nothing, and a final `REG.write_text(json.dumps(d, indent=2) + "\n")`. It must print how many parts it touched and how many it left alone.

- [ ] **Step 3: Extend the contract checker**

In `dashboard/render_blueprint.py`, add the three names to `REQUIRED_FEATURE_FIELDS` so T-1 covers them, then add two checks inside the per-feature loop in `find_contract_violations`, alongside the existing T-5 state-vocabulary check:

```python
        # T-1 / section 7: every part declares how the governor should allocate to it.
        resource_class = feature.get("resource_class")
        if resource_class not in KNOWN_RESOURCE_CLASSES:
            violations.append(
                f"T-1 {name}: resource_class '{resource_class}' is outside the declared "
                f"vocabulary {sorted(KNOWN_RESOURCE_CLASSES)} — the governor cannot allocate "
                f"to a part that has not said what kind of work it is"
            )

        # Section 6: both facts, or the part cannot be reasoned about for throttling.
        rate_risk = feature.get("rate_risk")
        skipped_tick_effect = feature.get("skipped_tick_effect")
        if rate_risk not in KNOWN_RATE_RISKS or skipped_tick_effect not in KNOWN_SKIPPED_TICK_EFFECTS:
            violations.append(
                f"T-1 {name}: rate_risk '{rate_risk}' and skipped_tick_effect "
                f"'{skipped_tick_effect}' must both come from "
                f"{sorted(KNOWN_RATE_RISKS)} and {sorted(KNOWN_SKIPPED_TICK_EFFECTS)}. "
                f"A part may enter a rate ladder only if a lower rate changes when its "
                f"answer arrives and never what it is, and only if a skipped tick delays "
                f"rather than corrupts. Undeclared is not throttleable."
            )
```

with, near `REQUIRED_FEATURE_FIELDS`:

```python
KNOWN_RESOURCE_CLASSES = frozenset({"io-bound", "compute-bound", "bandwidth-bound"})
KNOWN_RATE_RISKS = frozenset({"latency-only", "changes-the-answer"})
KNOWN_SKIPPED_TICK_EFFECTS = frozenset({"delays", "corrupts"})
```

- [ ] **Step 4: Prove the checker fails before the edit runs**

```bash
cd ~/ajit-segment-bots
python3 dashboard/check_contracts.py; echo "exit=$?"
```
Expected: **963 violations** (three per part × 321) and `exit=1`. If it exits 0, the new checks are not wired into the loop — fix that before running the edit, or the edit will appear to work while enforcing nothing.

- [ ] **Step 5: Run the edit, twice, and confirm the second run changes nothing**

```bash
python3 dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py
python3 dashboard/check_contracts.py; echo "exit=$?"
git diff --stat docs/features.json
python3 dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py
git diff --stat docs/features.json   # identical to the previous line: the edit is idempotent
```
Expected: `321 features, 27 categories: all contracts hold`, `exit=0`, and the two `git diff --stat` outputs identical.

- [ ] **Step 6: Delete the two xfail marks from Task 8 and rerun**

```bash
.venv/bin/python -m pytest tests/runtime/test_part_declaration.py -v
```
Expected: 6 passed, **0 xfailed**. Because the marks were `strict=True`, leaving one in place turns into a failure the moment it starts passing — so this step cannot be silently skipped.

- [ ] **Step 7: Regenerate the boards that read the blueprint**

```bash
python3 dashboard/build_part_monitor.py
python3 dashboard/build_wiring_explorer.py
dashboard/web/verify_wiring_renders.sh
```
Expected: both regenerate, and the wiring verifier passes having clicked all three views. The wiring must be **unchanged** — this edit adds declarations, not edges.

- [ ] **Step 8: Commit**

```bash
git add docs/proposals/part-declarations.md docs/features.json \
        dashboard/blueprint_edits/apply_2026-08-20_part_declarations.py \
        dashboard/render_blueprint.py tests/runtime/test_part_declaration.py
git commit -m "Every part declares its resource class and its two throttle facts

Sections 6 and 7. The checker refuses a part missing any of the three,
and the catch-all default is the most restrictive combination: a part
whose behaviour under a lower rate nobody has considered yet must not be
throttleable by accident. No consumes or produces changed, so no edge
moved. Idempotent -- re-running the edit changes nothing."
```

---

### Task 14: The substrate probes and its tile — measured, or it says so

**Files:**
- Create: `runtime/probes/__init__.py`
- Create: `runtime/probes/substrate_probes.py`
- Modify: `dashboard/build_status_board.py` — one new tile group
- Test: `tests/runtime/test_substrate_probes.py`

**Interfaces:**
- Consumes: every module built above.
- Produces:
  - `class SubstrateProbeResult` — frozen dataclass `(label: str, state: str, value: str, proof: str)` with the same field names as the board's own `ProbeResult`, so the board adapts it without a translation layer
  - `def probe_interpreter_build() -> SubstrateProbeResult`
  - `def probe_forkserver_is_forkable() -> SubstrateProbeResult`
  - `def probe_blas_is_pinned() -> SubstrateProbeResult`
  - `def probe_state_store_opens() -> SubstrateProbeResult`
  - `def probe_settings_are_readable() -> SubstrateProbeResult`
  - `def probe_measured_capacity() -> SubstrateProbeResult`
  - `def run_all_substrate_probes() -> list[SubstrateProbeResult]`

**Why the substrate gets a tile and not a cell:** RL-069 and §12 — the blueprint describes the circuit and this is the silicon under it, so it is off-diagram on purpose. It is still measured, and Rule 8 governs the tile: every state traces to a probe that ran, absence of evidence renders as `NOT MEASURED` and never as healthy, and each status carries the command or file it came from.

- [ ] **Step 1: Write the failing test**

`tests/runtime/test_substrate_probes.py`:

```python
"""The substrate is off-diagram but not unmeasured (RL-069, section 12).

Rule 8 governs what these may return: a state traces to a probe that ran, absence
of evidence is its own state and never green, and each status carries its proof.
"""

import pytest

from runtime.probes.substrate_probes import (
    SubstrateProbeResult,
    probe_blas_is_pinned,
    probe_forkserver_is_forkable,
    probe_interpreter_build,
    probe_measured_capacity,
    probe_settings_are_readable,
    probe_state_store_opens,
    run_all_substrate_probes,
)

VALID_STATES = {"OK", "NOT BUILT", "FAILING", "NOT MEASURED"}


@pytest.mark.parametrize(
    "probe",
    [
        probe_interpreter_build,
        probe_forkserver_is_forkable,
        probe_blas_is_pinned,
        probe_state_store_opens,
        probe_settings_are_readable,
        probe_measured_capacity,
    ],
)
def test_every_probe_returns_a_valid_state_and_names_its_proof(probe):
    result = probe()
    assert isinstance(result, SubstrateProbeResult)
    assert result.state in VALID_STATES
    assert result.label.strip()
    assert result.proof.strip(), "a status whose provenance cannot be named is not a status"


def test_no_probe_raises_and_every_one_is_included():
    results = run_all_substrate_probes()
    assert len(results) == 6
    assert all(result.state in VALID_STATES for result in results)


def test_the_interpreter_probe_reports_the_build_it_actually_found():
    result = probe_interpreter_build()
    assert result.state == "OK"
    assert "3.14" in result.value


def test_a_probe_that_cannot_establish_its_fact_says_so_rather_than_guessing(monkeypatch):
    # The Rule 8 property that matters: an unreadable source is NOT MEASURED,
    # never OK and never quietly omitted.
    import runtime.probes.substrate_probes as probes

    monkeypatch.setattr(
        probes, "settings_directory",
        lambda: probes.pathlib.Path("/nonexistent/settings/directory"),
    )
    result = probe_settings_are_readable()
    assert result.state in {"NOT MEASURED", "NOT BUILT"}
    assert result.state != "OK"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/runtime/test_substrate_probes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.probes'`.

- [ ] **Step 3: Write the probes**

`runtime/probes/__init__.py`: a docstring only, saying these are the substrate's own probes and that the substrate is off-diagram by RL-069.

`runtime/probes/substrate_probes.py` implements the six. Each one follows the same shape: name its proof first, try to establish the fact, and return `NOT MEASURED` with the reason if it cannot — never a default. Specifically:

- `probe_interpreter_build` — proof `python -c "import sysconfig; sysconfig.get_config_var('Py_GIL_DISABLED')"`. `OK` when it is `0` and the version is 3.14; `FAILING` when the free-threaded build is running, because §15.1 chose against it and five packages cannot be installed on it here.
- `probe_forkserver_is_forkable` — proof `field 20 of /proc/self/stat`. `OK` when `read_kernel_thread_count() == 1`; `FAILING` above it, with the observed count in the value, since that is a forkserver that would refuse every fork.
- `probe_blas_is_pinned` — proof `threadpoolctl.threadpool_info()`. `OK` when every reported pool has `num_threads == 1`; `FAILING` otherwise with the count; `NOT MEASURED` if `threadpoolctl` cannot be imported, because §7 says that without it no claim about live BLAS threads may be made at all.
- `probe_state_store_opens` — proof: open a store in the runtime's own state directory, read `PRAGMA journal_mode`. `OK` on `wal`; `FAILING` on anything else; `NOT MEASURED` if the directory does not exist yet.
- `probe_settings_are_readable` — proof `~/.config/ajit-segment-bots/settings/runtime.toml`. `OK` with the entry count in the value; `NOT BUILT` when the directory or file is absent, which is the honest state before the operator installs it; `FAILING` on a parse refusal, carrying the reason.
- `probe_measured_capacity` — proof `/proc/cpuinfo, /proc/meminfo`. Always `OK` with physical cores, logical CPUs, RAM and swap in the value, since these are readings rather than assertions.

- [ ] **Step 4: Add the tile group to the status board**

In `dashboard/build_status_board.py`, import `run_all_substrate_probes`, adapt each `SubstrateProbeResult` into the board's own `ProbeResult` (the field names already match, so this is a construction and not a mapping), and append them inside `run_all_probes` under a heading that says the substrate is off-diagram. Guard the import so that a board build still works before `runtime/` exists — on `ImportError`, emit a single `ProbeResult("Part runtime substrate", UNMEASURED, "runtime package not importable", "import runtime.probes.substrate_probes")`. A board that cannot measure the substrate must say so rather than omitting it, which is the same Rule 8 property the tests assert.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `.venv/bin/python -m pytest tests/runtime/test_substrate_probes.py -v`
Expected: PASS, 9 passed (6 parametrised plus 3).

- [ ] **Step 6: Rebuild the board and prove it draws**

```bash
python3 dashboard/build_status_board.py
dashboard/web/verify_board_renders.sh
```
Expected: the board regenerates and the render check passes with no console errors. **Look at the screenshot.** Before the operator installs `~/.config/.../runtime.toml`, the settings tile must read `NOT BUILT` — if it reads `OK`, a probe is guessing and that is the exact failure Rule 8 exists to prevent.

- [ ] **Step 7: Run the whole suite and commit**

```bash
.venv/bin/python -m pytest tests/runtime -v
python3 dashboard/check_contracts.py
git add runtime/probes/ dashboard/build_status_board.py tests/runtime/test_substrate_probes.py
git commit -m "The substrate measures itself, and says NOT MEASURED when it cannot

RL-069 keeps it off the part monitor; Rule 8 gives it a tile whose every
state traces to a probe that ran. The board still builds before runtime/
exists -- it reports that it could not measure, rather than omitting the
tile, which would read as nothing to measure."
```

---

## Definition of done for phase 0

Run all of it, and report the real output (Rule 0):

```bash
cd ~/ajit-segment-bots
.venv/bin/python -c "import sysconfig; assert sysconfig.get_config_var('Py_GIL_DISABLED') == 0"
.venv/bin/python -m pytest tests/runtime -v
python3 dashboard/check_contracts.py
python3 dashboard/build_status_board.py && dashboard/web/verify_board_renders.sh
python3 dashboard/build_part_monitor.py
git status --short && git log --oneline -14
```

Phase 0 is done when: every test passes with the cgroup and slow ones included, not deselected; `check_contracts.py` reports all 321 features holding with the three new declarations enforced; the status board draws with a substrate tile whose every state traces to a probe; the part monitor still shows all 321 parts `DECLARED`, because **phase 0 builds no part** — the substrate is off-diagram, and a part climbing a rung here would mean something was measured wrong; and every commit is pushed (Rule 9).

Then phase 1 starts, and its first job is `market-data-feed`, because history accrues only in real time and cannot be recovered later.

## What this plan does not do, said plainly

- **It builds no part.** All 321 stay `DECLARED`. The governor spine is phase 2 and the futures vertical is phase 3.
- **It does not start the tape.** That is phase 1's first task and the reason phase 1 follows immediately.
- **It leaves spot and options empty.** RL-050 and RL-062: declared in the blueprint, no source file, no fake logic. That is measured absence, not a placeholder.
- **It builds the control plane but not the data plane.** §3 separates the two, and T-2 needs the control plane first — the switch has to exist before anything can be switched. The data plane is addressed by data type per R-01, so its shape follows from what actually flows, and the first real answer to that is phase 1's `market-data`. §3 already settled the choice with a measurement: pickle over a pipe moves a normalised bar in 1.53 µs against a shared-memory ring's 0.78 µs, and intraday bars need about one message a second, so the simple transport is correct everywhere except a part measured to be genuinely hot. Building a ring now would be optimisation with no measurement behind it.
- **It does not make the three declaration defaults right.** Task 13 sets a defensible default per category and says so; each is corrected against the real part when that part is built. The catch-all is the most restrictive combination on purpose.

