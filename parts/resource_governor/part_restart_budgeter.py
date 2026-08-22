"""part-restart-budgeter: hold off a part that is crash-looping."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-restart-budgeter"

PART_DECLARATION = PartDeclaration(
    part_id="part-restart-budgeter",
    consumes=("restart-request", "part-fault"),
    produces=("restart-budget", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

GRANTED = "granted"
EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class RestartBudget:
    part_id: str
    verdict: str
    restarts_in_window: int
    allowance: int
    seconds_until_allowance_returns: float | None
    reason: str
    decided_at_ns: int


@dataclass
class BudgeterStanding:
    granted: int = 0
    refused: int = 0
    parts_held: set = field(default_factory=set)


class PartRestartBudgeter:
    """Grants restarts until a part has spent its allowance for the window.

    A crash loop is the failure this exists for: a part that dies on start and is
    restarted forever burns the machine and hides its own cause, because nothing
    survives long enough to be looked at. Refusing is not giving up -- the
    allowance returns as the window slides, so a part that was merely unlucky
    comes back on its own.
    """

    def __init__(self, restarts_allowed: int, window_seconds: float, monotonic=time.monotonic, now_ns=time.time_ns) -> None:
        self._allowed = restarts_allowed
        self._window = window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._restarts: dict[str, list[float]] = {}
        self.standing = BudgeterStanding()

    def request_restart(self, part_id: str) -> RestartBudget:
        now = self._monotonic()
        history = [at for at in self._restarts.get(part_id, []) if now - at < self._window]
        self._restarts[part_id] = history

        if len(history) >= self._allowed:
            oldest = min(history)
            self.standing.refused += 1
            self.standing.parts_held.add(part_id)
            return RestartBudget(
                part_id=part_id,
                verdict=EXHAUSTED,
                restarts_in_window=len(history),
                allowance=self._allowed,
                seconds_until_allowance_returns=self._window - (now - oldest),
                reason=f"{len(history)} restarts in the last {self._window:.0f}s",
                decided_at_ns=self._now_ns(),
            )

        history.append(now)
        self.standing.granted += 1
        self.standing.parts_held.discard(part_id)
        return RestartBudget(
            part_id=part_id,
            verdict=GRANTED,
            restarts_in_window=len(history),
            allowance=self._allowed,
            seconds_until_allowance_returns=None,
            reason="within the allowance for this window",
            decided_at_ns=self._now_ns(),
        )

    def read_budgets(self) -> tuple[RestartBudget, ...]:
        now = self._monotonic()
        budgets = []
        for part_id in sorted(self._restarts):
            history = [at for at in self._restarts[part_id] if now - at < self._window]
            self._restarts[part_id] = history
            exhausted = len(history) >= self._allowed
            budgets.append(
                RestartBudget(
                    part_id=part_id,
                    verdict=EXHAUSTED if exhausted else GRANTED,
                    restarts_in_window=len(history),
                    allowance=self._allowed,
                    seconds_until_allowance_returns=(
                        self._window - (now - min(history)) if exhausted else None
                    ),
                    reason="current standing, not a decision",
                    decided_at_ns=self._now_ns(),
                )
            )
        return tuple(budgets)


def describe_budgets(budgeter: PartRestartBudgeter) -> dict:
    return {
        "part_id": PART_ID,
        "granted": budgeter.standing.granted,
        "refused": budgeter.standing.refused,
        "parts_held": sorted(budgeter.standing.parts_held),
    }


def run_part_restart_budgeter(
    budgeter: PartRestartBudgeter, control_socket, read_requests, publish_budgets,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for part_id in read_requests():
            budgeter.request_restart(part_id)
        publish_budgets(budgeter.read_budgets())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
