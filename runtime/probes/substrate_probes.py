"""The substrate measures itself, and says NOT MEASURED when it cannot (RL-069).

Section 12 of the runtime spec and RL-069 keep this package off the blueprint: the
321 declared parts describe the circuit, and everything in runtime/ is the silicon
underneath it. Off-diagram is not the same as unmeasured, so this module gives the
status board six facts about that silicon, each carrying the same discipline Rule 8
demands of every tile on it:

    name the proof first, try to establish the fact, and return NOT MEASURED with
    the reason if it cannot -- never a default, and never a guess standing in for a
    reading that did not happen.

Every probe here is written to that shape and never raises: a probe that cannot
establish its fact reports NOT MEASURED rather than letting the caller discover the
gap as a crash.
"""

from __future__ import annotations

import os
import pathlib
import re
import sqlite3
import sys
import sysconfig
import tomllib
from dataclasses import dataclass

from runtime.forkserver_launcher import SINGLE_THREAD, read_kernel_thread_count
from runtime.hardware_facts import MeminfoFieldMissing, measure_hardware_facts
from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)
from runtime.state_store import StoreDurability, open_store
from runtime.storage_facts import VolatileStorageRefused

# Same four states the board's own ProbeResult uses (dashboard/build_status_board.py),
# kept as separate literals rather than imported from there: the board depends on
# this package, not the other way round, and Task 14's own ImportError guard exists
# precisely so the board can still build when this module is the thing missing.
OK = "OK"
NOT_BUILT = "NOT BUILT"
FAILING = "FAILING"
NOT_MEASURED = "NOT MEASURED"

# runtime/probes/substrate_probes.py -> runtime/probes -> runtime -> repository root.
PYPROJECT_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "pyproject.toml"

# The filename this probe's own state-store reading and writes under, inside the
# runtime's own state directory. A fixed, stable name so repeated probe runs read
# and extend the same store rather than scattering one file per run.
_PROBE_STORE_FILENAME = "substrate-probe.sqlite3"


class _PinnedVersionUnreadable(Exception):
    """pyproject.toml's own requires-python could not be read or parsed."""


def _read_pinned_interpreter_version() -> tuple[int, int]:
    """The (major, minor) this project actually pins, read from pyproject.toml's
    own requires-python rather than duplicated as a literal in this module.

    RL-061: probe_interpreter_build's OK/FAILING threshold is a number in decision
    code, so it has to carry its provenance -- and the one place that provenance
    can come from without inventing a second source of truth is the same
    requires-python D-011 and section 15.1 already pin this project to. If this
    file and pyproject.toml ever disagree, that disagreement should be visible on
    the tile rather than silently resolved by whichever one got hardcoded here.
    """
    try:
        body = PYPROJECT_PATH.read_text()
    except OSError as failure:
        raise _PinnedVersionUnreadable(f"{PYPROJECT_PATH} could not be read: {failure}") from failure
    try:
        document = tomllib.loads(body)
    except tomllib.TOMLDecodeError as failure:
        raise _PinnedVersionUnreadable(f"{PYPROJECT_PATH} is not valid TOML: {failure}") from failure

    requires_python = document.get("project", {}).get("requires-python")
    if not requires_python:
        raise _PinnedVersionUnreadable(f"{PYPROJECT_PATH} declares no [project] requires-python")

    match = re.search(r"(\d+)\.(\d+)", requires_python)
    if not match:
        raise _PinnedVersionUnreadable(
            f"{PYPROJECT_PATH} requires-python {requires_python!r} names no major.minor version"
        )
    return (int(match.group(1)), int(match.group(2)))


@dataclass(frozen=True)
class SubstrateProbeResult:
    """One measured fact about the runtime substrate, and the evidence it came from.

    Same field names as the board's ProbeResult on purpose (Task 14's interface),
    so the board adapts this without a translation layer.
    """

    label: str
    state: str
    value: str
    proof: str


def _runtime_state_directory() -> pathlib.Path:
    """Where structured part state lives.

    The XDG_STATE_HOME counterpart to settings_reader.settings_directory's
    XDG_CONFIG_HOME lookup: settings are the operator's input, this is the
    runtime's own output, and XDG keeps the two apart by convention rather than
    by an ad hoc path this module would otherwise have to invent.
    """
    state_home = os.environ.get("XDG_STATE_HOME")
    root = pathlib.Path(state_home) if state_home else pathlib.Path.home() / ".local" / "state"
    return root / "ajit-segment-bots"


def _read_runtime_setting(name: str) -> object:
    """RL-061: a number this module needs carries its provenance from the operator's
    own settings file rather than being written into this module as a literal."""
    document = load_settings_document(settings_directory() / "runtime.toml", scope="runtime")
    return document.read_value(name)


def probe_interpreter_build() -> SubstrateProbeResult:
    """Is this the standard CPython build section 15.1 chose, at the version pinned?"""
    label = "Interpreter build"
    proof = (
        "python -c \"import sysconfig; sysconfig.get_config_var('Py_GIL_DISABLED')\"; "
        f"requires-python in {PYPROJECT_PATH}"
    )
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    gil_disabled = sysconfig.get_config_var("Py_GIL_DISABLED")

    if gil_disabled is None:
        return SubstrateProbeResult(
            label, NOT_MEASURED, f"Py_GIL_DISABLED unreadable on python {version}", proof
        )
    if gil_disabled:
        return SubstrateProbeResult(
            label,
            FAILING,
            f"free-threaded build running (python {version}) -- section 15.1 chose the "
            f"standard build because five pinned packages ship no cp314t wheel",
            proof,
        )

    try:
        pinned_major, pinned_minor = _read_pinned_interpreter_version()
    except _PinnedVersionUnreadable as failure:
        return SubstrateProbeResult(label, NOT_MEASURED, str(failure), proof)

    if sys.version_info[:2] != (pinned_major, pinned_minor):
        return SubstrateProbeResult(
            label,
            FAILING,
            f"standard build but python {version}, pyproject.toml pins {pinned_major}.{pinned_minor}",
            proof,
        )
    return SubstrateProbeResult(
        label, OK, f"standard build, python {version}, matches pyproject.toml's {pinned_major}.{pinned_minor} pin", proof
    )


def probe_forkserver_is_forkable() -> SubstrateProbeResult:
    """Is this process at or under the kernel-thread ceiling the forkserver enforces?

    The ceiling comes from the operator's own fork_thread_ceiling setting rather
    than a literal here -- it is the same number spawn_part (forkserver_launcher.py)
    refuses above, so this probe measures the live process against the actual
    configured invariant instead of a value duplicated from it.
    """
    label = "Forkserver is forkable"
    proof = "field 20 of /proc/self/stat (forkserver_launcher.read_kernel_thread_count)"
    try:
        thread_ceiling = _read_runtime_setting("fork_thread_ceiling")
    except (SettingsParseRefused, KeyError, OSError) as failure:
        return SubstrateProbeResult(
            label, NOT_MEASURED, f"fork_thread_ceiling unreadable: {failure}", proof
        )
    try:
        observed = read_kernel_thread_count()
    except (OSError, ValueError, IndexError) as failure:
        return SubstrateProbeResult(label, NOT_MEASURED, f"/proc/self/stat unreadable: {failure}", proof)

    if observed <= thread_ceiling:
        return SubstrateProbeResult(label, OK, f"{observed} kernel thread(s), ceiling {thread_ceiling}", proof)
    return SubstrateProbeResult(
        label,
        FAILING,
        f"{observed} kernel threads, ceiling {thread_ceiling} -- a forkserver this wide "
        f"would refuse every fork",
        proof,
    )


def probe_blas_is_pinned() -> SubstrateProbeResult:
    """Is every live BLAS thread pool pinned to the single thread the caps set?

    Compares against SINGLE_THREAD, the exact value apply_blas_thread_caps writes
    into every BLAS_THREAD_CAP_VARIABLES entry (forkserver_launcher.py) -- so this
    checks whether those caps actually took effect, not an independently chosen number.
    """
    label = "BLAS threads pinned"
    proof = "threadpoolctl.threadpool_info()"
    try:
        import threadpoolctl
    except ImportError as failure:
        return SubstrateProbeResult(
            label, NOT_MEASURED, f"threadpoolctl not importable: {failure}", proof
        )

    try:
        pools = threadpoolctl.threadpool_info()
    except Exception as failure:  # threadpoolctl inspects loaded C libraries by name
        return SubstrateProbeResult(label, NOT_MEASURED, f"threadpool_info() raised: {failure}", proof)

    if not pools:
        # "every one of zero pools is pinned" is vacuously true and not a reading --
        # nothing has imported a BLAS library into this process yet, so the question
        # has no live subject. This is not a failure and the caps are not broken; a
        # process that goes on to import numpy is the one this tile is measuring.
        return SubstrateProbeResult(
            label,
            NOT_MEASURED,
            "0 pools reported -- no BLAS library is loaded in this process, so there is "
            "nothing to speak for",
            proof,
        )

    pinned_thread_count = int(SINGLE_THREAD)
    unpinned = [pool for pool in pools if pool.get("num_threads") != pinned_thread_count]
    if unpinned:
        observed = ", ".join(f"{pool.get('internal_api')}={pool.get('num_threads')}" for pool in unpinned)
        return SubstrateProbeResult(
            label,
            FAILING,
            f"{len(unpinned)} of {len(pools)} pool(s) not pinned to {pinned_thread_count} thread: {observed}",
            proof,
        )
    return SubstrateProbeResult(
        label, OK, f"{len(pools)} pool(s) reported, all pinned to {pinned_thread_count} thread", proof
    )


def probe_state_store_opens() -> SubstrateProbeResult:
    """Does the runtime's own state directory hold a store that opens in WAL mode?"""
    label = "State store opens"
    state_directory = _runtime_state_directory()
    store_path = state_directory / _PROBE_STORE_FILENAME
    proof = f"open_store({store_path}); PRAGMA journal_mode"

    if not state_directory.is_dir():
        return SubstrateProbeResult(label, NOT_MEASURED, f"{state_directory} does not exist yet", proof)

    try:
        busy_timeout_seconds = _read_runtime_setting("store_busy_timeout")
    except (SettingsParseRefused, KeyError, OSError) as failure:
        return SubstrateProbeResult(
            label, NOT_MEASURED, f"store_busy_timeout unreadable: {failure}", proof
        )

    try:
        connection = open_store(store_path, StoreDurability.RECORD, busy_timeout_seconds)
    except (VolatileStorageRefused, sqlite3.Error, OSError) as failure:
        return SubstrateProbeResult(label, NOT_MEASURED, f"open_store refused: {failure}", proof)

    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    if journal_mode == "wal":
        return SubstrateProbeResult(label, OK, "journal_mode=wal", proof)
    return SubstrateProbeResult(label, FAILING, f"journal_mode={journal_mode}", proof)


def probe_settings_are_readable() -> SubstrateProbeResult:
    """Does the operator's live runtime.toml parse, and how many entries does it carry?"""
    label = "Settings are readable"
    runtime_toml = settings_directory() / "runtime.toml"
    proof = str(runtime_toml)

    if not runtime_toml.is_file():
        return SubstrateProbeResult(label, NOT_BUILT, f"{runtime_toml} absent", proof)

    try:
        document = load_settings_document(runtime_toml, scope="runtime")
    except SettingsParseRefused as failure:
        return SubstrateProbeResult(label, FAILING, str(failure), proof)

    return SubstrateProbeResult(
        label, OK, f"{len(document.entries)} entries, digest {document.content_digest[:12]}", proof
    )


def probe_measured_capacity() -> SubstrateProbeResult:
    """What does the kernel say this machine has, right now?

    Rule 8: a partial reading is more useful than a bare refusal, but only while
    its state stays honest. physical_cores, logical_cpus, and numa_nodes each
    come back None when hardware_facts could not measure them (no cpuinfo
    topology, no NUMA sysfs tree) -- and a tile that reported OK over a None
    would be a number nobody measured wearing the shape of one. So this still
    reports every field it read, but the state is NOT MEASURED, naming which
    fields were unmeasurable, whenever any of the three is None.
    """
    label = "Measured capacity"
    proof = "/proc/cpuinfo, /proc/meminfo (hardware_facts.measure_hardware_facts)"
    try:
        facts = measure_hardware_facts()
    except (MeminfoFieldMissing, OSError) as failure:
        return SubstrateProbeResult(label, NOT_MEASURED, str(failure), proof)

    value = (
        f"physical_cores={facts.physical_cores}, logical_cpus={facts.logical_cpus}, "
        f"numa_nodes={facts.numa_nodes}, ram={facts.total_ram_bytes}B, "
        f"swap={facts.swap_total_bytes}B"
    )
    unmeasured_fields = [
        field_name
        for field_name, field_value in (
            ("physical_cores", facts.physical_cores),
            ("logical_cpus", facts.logical_cpus),
            ("numa_nodes", facts.numa_nodes),
        )
        if field_value is None
    ]
    if unmeasured_fields:
        return SubstrateProbeResult(
            label,
            NOT_MEASURED,
            f"{', '.join(unmeasured_fields)} could not be measured on this machine; "
            f"{value}",
            proof,
        )
    return SubstrateProbeResult(label, OK, value, proof)


_ALL_PROBES = (
    probe_interpreter_build,
    probe_forkserver_is_forkable,
    probe_blas_is_pinned,
    probe_state_store_opens,
    probe_settings_are_readable,
    probe_measured_capacity,
)


def run_all_substrate_probes() -> list[SubstrateProbeResult]:
    """Every substrate probe, run once. A probe that raises is unmeasured, not healthy."""
    results = []
    for probe in _ALL_PROBES:
        try:
            results.append(probe())
        except Exception as error:  # belt under the individual probes' own guards
            results.append(
                SubstrateProbeResult(
                    probe.__name__,
                    NOT_MEASURED,
                    f"probe raised {type(error).__name__}: {error}",
                    probe.__name__,
                )
            )
    return results
