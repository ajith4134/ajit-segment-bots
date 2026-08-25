"""Start parts and stop them. Substrate, not a feature (RL-069).

This is off-diagram: it has no PART_DECLARATION, never appears on the part monitor,
and gets its own status-board tile. It owns exactly what a part cannot own --

    the forkserver, and the refusal that keeps forking safe;
    the control socket pair per part, the governor's end held here and the part's
      end passed across the fork, which makes T-2 a fact of the kernel's descriptor
      table rather than a convention;
    the unlink of a stale inbox address before a part binds it, because a socket
      file outlives the process that bound it and only this component knows a part
      is not currently running;
    placement in a transient scope, confirmed by reading /proc rather than by
      trusting a return code that reports success before the move is attempted.

**It decides nothing.** It starts what it is told to start and stops what it is
told to stop. Deciding belongs to gate-actuator, which is a part, and which reaches
this over the control plane like everything else. A launcher that chose would be a
second governor, and T-2 permits one.

Measured on this box with all 321 parts at once: 3.46 ms median to fork, 1.17 s to
start the whole system, 0.11 s to stop it, 5.46 MB PSS each, and every byte back
twelve seconds after the last one exited.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from runtime.control_channel import COMMAND_TURN_OFF, create_control_socket_pair, send_command
from runtime.forkserver_launcher import apply_blas_thread_caps, spawn_part, start_forkserver
from runtime.part_context import open_part_context
from runtime.scope_placer import PlacementNotConfirmed, ScopeLimits, place_process_in_scope
from runtime.switch_service import SwitchService, find_switch_actuator, switch_endpoint_address
from runtime.wiring_plan import PartWiring, create_inbox_root, derive_wiring, inbox_root, load_blueprint

PARTS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "parts"

# The entry point every part carries. One name for all 321: a launcher that had to
# know which function to call for which part would be the circuit knowing its parts.
PART_ENTRY_POINT = "start_part"

# What the forkserver imports before any part is forked from it. Widening it does
# not save memory -- measured, going from 3 modules to 15 changed the per-part cost
# by 0.01 MB, because the cost is the interpreter's own private pages rather than the
# imports -- so this is the set every part reaches for, plus numpy.
#
# numpy is preloaded for a different reason than memory: it is imported by the tape
# and by durable numeric state, it is the slowest import in the tree, and the
# forkserver's preload is fixed by whoever starts it first. A launcher that omitted
# it would leave every part paying that import itself, and would also decide the
# question for any other component in the process that expected it preloaded.
# start_forkserver applies the BLAS thread caps before importing it, which is what
# makes the forkserver forkable at all.
FORKSERVER_PRELOAD = (
    "numpy",
    "runtime.part_process",
    "runtime.control_channel",
    "runtime.part_declaration",
)

EXIT_CODE_NEVER_RETURNED = None


class PartHasNoModule(LookupError):
    """No source file is named for this part, so there is nothing to start."""


class PartHasNoEntryPoint(AttributeError):
    """The part's module exists and does not carry start_part."""


class PartAlreadyRunning(RuntimeError):
    """A part is one process. Starting a second copy would give it two of everything."""


@dataclass
class LaunchedPart:
    """One running part, and the evidence it is running as the governor intended."""

    part_id: str
    process: multiprocessing.process.BaseProcess
    governor_control_socket: object
    scope_directory: pathlib.Path | None
    placement_refusal: str | None
    started_at_ns: int

    @property
    def is_running(self) -> bool:
        return self.process.is_alive()

    @property
    def is_bounded(self) -> bool:
        """Is this part inside its own scope, where the governor can limit it?

        A part that is not is a part the governor believes it has bounded and has
        not -- so this is reported rather than assumed, and never rendered green.
        """
        return self.scope_directory is not None


@dataclass
class LauncherStanding:
    """What the launcher has actually done, for the board's substrate tile."""

    started: int = 0
    stopped: int = 0
    refused_to_start: int = 0
    unbounded: int = 0
    stops_that_needed_a_kill: int = 0
    last_refusal: str | None = None
    fork_milliseconds: list[float] = field(default_factory=list)


def resolve_part_module(part_id: str, parts_root: pathlib.Path = PARTS_ROOT) -> str:
    """The module that implements a part, found by the file named for it.

    Exact stem, never a substring: `order-state-poller` and `order-state-poller-v2`
    would otherwise resolve to the same file, and the wrong one would run without
    anything saying so.
    """
    stem = part_id.replace("-", "_")
    matches = sorted(path for path in parts_root.rglob(f"{stem}.py") if path.stem == stem)
    if not matches:
        raise PartHasNoModule(
            f"no source file named for part '{part_id}' under {parts_root} (looked for "
            f"'{stem}.py'). A part with no module is DECLARED, not startable -- which is "
            f"what its rung on the board already says."
        )
    if len(matches) > 1:
        raise PartHasNoModule(
            f"part '{part_id}' is implemented by more than one file: "
            f"{[str(path) for path in matches]}. One part, one file."
        )
    relative = matches[0].relative_to(parts_root.parent).with_suffix("")
    return str(relative).replace(os.sep, ".")


def run_part_process(
    part_id: str,
    part_module_name: str,
    control_socket,
    runtime_directory_name: str | None,
    switch_endpoint: str | None = None,
    settings_directory_name: str | None = None,
) -> None:
    """The forked child's whole life: build the context, hand it to the part.

    Module-level on purpose -- forkserver pickles the target by qualified name, and
    a nested function fails with AttributeError on '__mp_main__'.

    The context is closed on the way out whatever happened, so a part that raises
    does not leave its inbox addresses bound by a process that is gone.
    """
    runtime_directory = pathlib.Path(runtime_directory_name) if runtime_directory_name else None
    # Passed rather than left to the child's environment. A forkserver is one
    # long-lived process and every part is forked from it, so a child's
    # XDG_CONFIG_HOME is whatever the forkserver was started with -- which makes
    # "which settings did this part read" a question about process history rather
    # than about the run. None means the operator's own directory.
    settings_directory_path = (
        pathlib.Path(settings_directory_name) if settings_directory_name else None
    )
    module = importlib.import_module(part_module_name)
    entry_point = getattr(module, PART_ENTRY_POINT, None)
    if entry_point is None:
        raise PartHasNoEntryPoint(
            f"{part_module_name} has no {PART_ENTRY_POINT}. Every part carries one, identically "
            f"named -- that sameness is what T-1 means, and it is what lets the launcher start "
            f"321 parts without knowing anything about any of them."
        )
    context = open_part_context(
        part_id=part_id,
        control_socket=control_socket,
        runtime_directory=runtime_directory,
        settings_directory_path=settings_directory_path,
        switch_endpoint=switch_endpoint,
    )
    try:
        entry_point(context)
    finally:
        context.close()


class PartLauncher:
    """Starts and stops parts. Holds the switch; decides nothing.

    place_in_scope is required rather than defaulted. A part outside its own scope
    is a part the governor cannot bound, and whether that is acceptable right now is
    the caller's statement to make -- so it is made out loud, and `standing()`
    reports how many parts are running unbounded rather than letting it pass.
    """

    def __init__(
        self,
        place_in_scope: bool,
        thread_ceiling: int,
        placement_confirmation_deadline_seconds: float,
        placement_confirmation_poll_interval_seconds: float,
        limits_for: Callable[[str], ScopeLimits] | None = None,
        runtime_directory: pathlib.Path | None = None,
        wiring: dict[str, PartWiring] | None = None,
        parts_root: pathlib.Path = PARTS_ROOT,
        settings_directory: pathlib.Path | None = None,
    ) -> None:
        if place_in_scope and limits_for is None:
            raise ValueError(
                "place_in_scope is True and no limits_for was given. A scope with no limits is "
                "a scope that bounds nothing; the limits come from the governor, which is the "
                "part of the system that decides what a part may have."
            )
        apply_blas_thread_caps()
        self._place_in_scope = place_in_scope
        self._thread_ceiling = thread_ceiling
        self._deadline_seconds = placement_confirmation_deadline_seconds
        self._poll_interval_seconds = placement_confirmation_poll_interval_seconds
        self._limits_for = limits_for
        self._runtime_directory = runtime_directory
        self._parts_root = parts_root
        # Which settings the parts this launcher starts will read. None is the
        # operator's own directory, which is what a real run wants; a caller that
        # states one is saying so out loud rather than arranging an environment
        # variable and hoping it survives the fork.
        self._settings_directory = settings_directory
        self._wiring = wiring if wiring is not None else derive_wiring(runtime_directory=runtime_directory)
        self._forkserver = start_forkserver(FORKSERVER_PRELOAD)
        self._running: dict[str, LaunchedPart] = {}
        self.standing = LauncherStanding()
        create_inbox_root(runtime_directory)
        # Which part may ask for a switch, decided by contract rather than by name:
        # whoever the blueprint says turns a switch-plan into switch-records.
        self._switch_actuator_part_id = find_switch_actuator(load_blueprint())
        # The last dead process per part, so its exit code survives being
        # forgotten from _running and a restart can say how the last one ended.
        self._last_exit: dict[str, LaunchedPart] = {}
        self._switch_service: SwitchService | None = None

    def open_switch_service(self, stop_deadline_seconds: float, backlog: int) -> SwitchService:
        """Bind the endpoint the blueprint's actuator reaches this launcher through.

        The launcher serves it and never initiates on it: the decision to switch a
        part belongs to gate-actuator, and this is only the component that can carry
        it out, because it is the one holding every control socket (T-2).
        """
        if self._switch_service is None:
            self._switch_service = SwitchService(
                address=switch_endpoint_address(inbox_root(self._runtime_directory)),
                turn_on=lambda part_id: f"pid {self.start(part_id).process.pid}",
                turn_off=lambda part_id: f"exit {self.stop(part_id, stop_deadline_seconds)}",
                backlog=backlog,
            )
        return self._switch_service

    def _switch_endpoint_for(self, part_id: str) -> str | None:
        """The endpoint address, and only to the one part entitled to it.

        Every other part is started with None, so it holds no way to reach the
        switch at all -- T-2 enforced by what a process was given rather than by
        what its code refrains from doing.
        """
        if self._switch_service is None or part_id != self._switch_actuator_part_id:
            return None
        return self._switch_service.address

    @property
    def running_part_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._running))

    def is_running(self, part_id: str) -> bool:
        launched = self._running.get(part_id)
        return launched is not None and launched.is_running

    def start(self, part_id: str) -> LaunchedPart:
        """Fork one part, place it, and confirm the placement by reading /proc."""
        if self.is_running(part_id):
            raise PartAlreadyRunning(
                f"'{part_id}' is already running as pid {self._running[part_id].process.pid}. A part "
                f"is one process (T-1); a second copy would bind the same inbox addresses and take "
                f"half its own input."
            )
        self._forget_if_dead(part_id)
        if part_id not in self._wiring:
            self.standing.refused_to_start += 1
            self.standing.last_refusal = f"{part_id}: not in the blueprint"
            raise PartHasNoModule(
                f"'{part_id}' is not in the blueprint, so it has no wiring and cannot be started. "
                f"A design change is a blueprint edit first, then code."
            )
        module_name = resolve_part_module(part_id, self._parts_root)
        self._unlink_stale_addresses(part_id)

        governor_end, part_end = create_control_socket_pair()
        forked_at = time.perf_counter()
        try:
            process = spawn_part(
                context=self._forkserver,
                entry_point=run_part_process,
                arguments=(
                    part_id,
                    module_name,
                    part_end,
                    str(self._runtime_directory) if self._runtime_directory else None,
                    self._switch_endpoint_for(part_id),
                    str(self._settings_directory) if self._settings_directory else None,
                ),
                thread_ceiling=self._thread_ceiling,
            )
        except Exception as refusal:
            governor_end.close()
            part_end.close()
            self.standing.refused_to_start += 1
            self.standing.last_refusal = f"{part_id}: {type(refusal).__name__}: {refusal}"
            raise
        self.standing.fork_milliseconds.append((time.perf_counter() - forked_at) * 1e3)
        part_end.close()  # the child owns its end now

        scope_directory, placement_refusal = self._place(part_id, process.pid)
        launched = LaunchedPart(
            part_id=part_id,
            process=process,
            governor_control_socket=governor_end,
            scope_directory=scope_directory,
            placement_refusal=placement_refusal,
            started_at_ns=time.time_ns(),
        )
        self._running[part_id] = launched
        self.standing.started += 1
        if not launched.is_bounded:
            self.standing.unbounded += 1
        return launched

    def stop(self, part_id: str, stop_deadline_seconds: float) -> int | None:
        """Send off, wait, and kill only if the part would not go.

        A part that has to be killed is a finding, not a tidy-up: T-3 says off is
        the process exiting, and a part that ignored its switch is one the governor
        could not switch.
        """
        launched = self._running.get(part_id)
        if launched is None:
            return EXIT_CODE_NEVER_RETURNED
        try:
            send_command(launched.governor_control_socket, COMMAND_TURN_OFF, {"reason": "governor"})
        except OSError:
            pass  # the part is already gone; joining below establishes that
        launched.process.join(timeout=stop_deadline_seconds)
        if launched.process.is_alive():
            launched.process.kill()
            launched.process.join(timeout=stop_deadline_seconds)
            self.standing.stops_that_needed_a_kill += 1
            self.standing.last_refusal = f"{part_id}: did not stop within {stop_deadline_seconds}s"
        launched.governor_control_socket.close()
        self._running.pop(part_id, None)
        self.standing.stopped += 1
        if not launched.is_bounded:
            self.standing.unbounded -= 1
        return launched.process.exitcode

    def stop_all(self, stop_deadline_seconds: float) -> dict[str, int | None]:
        """Switch everything off. Measured at 0.11 s for all 321."""
        return {part_id: self.stop(part_id, stop_deadline_seconds) for part_id in self.running_part_ids}

    def report(self) -> dict:
        """What is running, and what about it is not what the governor intended."""
        fork_times = sorted(self.standing.fork_milliseconds)
        return {
            "running": [
                {
                    "part_id": launched.part_id,
                    "pid": launched.process.pid,
                    "is_bounded": launched.is_bounded,
                    "scope": str(launched.scope_directory) if launched.scope_directory else None,
                    "placement_refusal": launched.placement_refusal,
                    "started_at_ns": launched.started_at_ns,
                }
                for launched in sorted(self._running.values(), key=lambda part: part.part_id)
            ],
            "started": self.standing.started,
            "stopped": self.standing.stopped,
            "refused_to_start": self.standing.refused_to_start,
            "running_unbounded": self.standing.unbounded,
            "stops_that_needed_a_kill": self.standing.stops_that_needed_a_kill,
            "last_refusal": self.standing.last_refusal,
            "fork_milliseconds_median": fork_times[len(fork_times) // 2] if fork_times else None,
            "switch_requests_served": self._switch_service.requests_served if self._switch_service else None,
            "switch_requests_refused": self._switch_service.requests_refused if self._switch_service else None,
            "switch_actuator": self._switch_actuator_part_id,
        }

    def _place(self, part_id: str, pid: int) -> tuple[pathlib.Path | None, str | None]:
        if not self._place_in_scope:
            return None, "not placed: this launcher was started without scope placement"
        try:
            directory = place_process_in_scope(
                pid=pid,
                scope_name=part_id,
                limits=self._limits_for(part_id),
                confirmation_deadline_seconds=self._deadline_seconds,
                poll_interval_seconds=self._poll_interval_seconds,
            )
        except PlacementNotConfirmed as refusal:
            # The part is running and unbounded. Not killed here: killing it would
            # be a decision, and this component does not make those. It is reported,
            # and it counts as unbounded until something that does decide acts.
            return None, str(refusal)
        return directory, None

    def _unlink_stale_addresses(self, part_id: str) -> None:
        """Remove inbox addresses a previous run of this part left behind.

        Only the launcher does this, and only for a part it is about to start: a
        socket file outlives its process, and unlinking one belonging to a part that
        is running would make that part unreachable while it kept running.
        """
        for address in self._wiring[part_id].inboxes.values():
            try:
                os.unlink(address)
            except FileNotFoundError:
                pass

    def read_exit_code(self, part_id: str) -> int | None:
        """How a part that is no longer running ended, or None if it still is.

        A supervisor that records only *that* a part restarted cannot tell a
        crash from a kill from a clean exit, and a part crash-looping thirteen
        times a minute looks the same in the log as one being cycled deliberately.
        Negative values are signals: -9 is SIGKILL, -11 a segfault. 1 is an
        uncaught exception, which is the case worth reading a traceback for.
        """
        launched = self._running.get(part_id) or self._last_exit.get(part_id)
        if launched is None:
            return None
        if launched.is_running:
            return None
        return launched.process.exitcode

    def _forget_if_dead(self, part_id: str) -> None:
        launched = self._running.get(part_id)
        if launched is not None and not launched.is_running:
            launched.governor_control_socket.close()
            # Kept, not dropped: the exit code is the only thing that says how it
            # died, and forgetting it is why a crash loop was unreadable.
            self._last_exit[part_id] = launched
            self._running.pop(part_id, None)
            if not launched.is_bounded:
                self.standing.unbounded -= 1

    def close(self) -> None:
        for launched in list(self._running.values()):
            launched.governor_control_socket.close()
        self._running.clear()
        if self._switch_service is not None:
            self._switch_service.close()
            self._switch_service = None

    def __enter__(self) -> PartLauncher:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()
