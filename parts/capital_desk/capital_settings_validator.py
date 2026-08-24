"""capital-settings-validator: are the settings consistent, or not.

Every downstream part treats the settings as authority. This is the one part that
asks whether that authority makes sense, and its verdict gates order placement --
`trade-capital-bounds-gate` refuses everything while the answer is no.

The checks are all of the form "these two settings cannot both be obeyed":

- a minimum per trade above the maximum -- no order satisfies both
- a maximum per trade above the whole allocation -- one trade could commit
  everything the segment has
- a leverage ceiling above what the chosen instrument allows -- the venue will
  reject it, after the decision has been made
- an allocation above the main balance -- money that is not there

**It never repairs.** Clamping a contradictory setting to something workable
would mean the system trades on numbers the operator never wrote, and the
settings file would stop being the record of what was intended.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import (
    CONSISTENT,
    INCOMPLETE,
    INCONSISTENT,
    CapitalSettingsVerdict,
    SettingsFault,
)

PART_ID = "capital-settings-validator"

PART_DECLARATION = PartDeclaration(
    part_id="capital-settings-validator",
    consumes=(
        "main-account-setting", "capital-allotment", "trade-capital-bounds",
        "leverage-ceiling", "instrument-choice",
    ),
    produces=("capital-settings-verdict", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

@dataclass
class ValidatorStanding:
    judgements: int = 0
    consistent: int = 0
    inconsistent: int = 0
    incomplete: int = 0
    faults_by_kind: dict = field(default_factory=dict)
    last_faults: tuple = ()


class CapitalSettingsValidator:
    """Judges one segment's capital settings consistent, or names every contradiction."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self.standing = ValidatorStanding()

    def judge(
        self,
        segment: str,
        allotment=None,
        main_balance: float | None = None,
        instrument_maximum_leverage: float | None = None,
    ) -> CapitalSettingsVerdict:
        self.standing.judgements += 1

        if allotment is None:
            self.standing.incomplete += 1
            return self._verdict(
                segment, INCOMPLETE, (),
                "no capital allotment has been read for this segment; nothing can be judged",
            )

        faults: list[SettingsFault] = []
        bounds = allotment.bounds

        if bounds.minimum_capital > bounds.maximum_capital:
            faults.append(
                SettingsFault(
                    settings=("minimum_capital_per_trade", "maximum_capital_per_trade"),
                    values=(bounds.minimum_capital, bounds.maximum_capital),
                    explanation=(
                        f"a minimum of {bounds.minimum_capital:,.2f} is above the maximum of "
                        f"{bounds.maximum_capital:,.2f}; no order size satisfies both"
                    ),
                )
            )

        if allotment.allotted > 0 and bounds.maximum_capital > allotment.allotted:
            faults.append(
                SettingsFault(
                    settings=("maximum_capital_per_trade", "allocated_balance"),
                    values=(bounds.maximum_capital, allotment.allotted),
                    explanation=(
                        f"one trade may use {bounds.maximum_capital:,.2f} of an allocation of "
                        f"{allotment.allotted:,.2f}; a single trade could commit everything"
                    ),
                )
            )

        if main_balance is not None and allotment.allotted > main_balance:
            faults.append(
                SettingsFault(
                    settings=("allocated_balance", "main_balance"),
                    values=(allotment.allotted, main_balance),
                    explanation=(
                        f"this segment is allocated {allotment.allotted:,.2f} of a main balance of "
                        f"{main_balance:,.2f}; the money is not there"
                    ),
                )
            )

        if allotment.leverage_ceiling < 1.0:
            faults.append(
                SettingsFault(
                    settings=("leverage_ceiling",),
                    values=(allotment.leverage_ceiling,),
                    explanation=(
                        f"a ceiling of {allotment.leverage_ceiling} is below unlevered; 1.0 is the floor"
                    ),
                )
            )

        if (
            instrument_maximum_leverage is not None
            and allotment.leverage_ceiling > instrument_maximum_leverage
        ):
            faults.append(
                SettingsFault(
                    settings=("leverage_ceiling", "instrument_maximum_leverage"),
                    values=(allotment.leverage_ceiling, instrument_maximum_leverage),
                    explanation=(
                        f"a ceiling of {allotment.leverage_ceiling}x is above the "
                        f"{instrument_maximum_leverage}x this instrument allows; the venue would "
                        f"reject the order after the decision was already made"
                    ),
                )
            )

        for fault in faults:
            key = "+".join(fault.settings)
            self.standing.faults_by_kind[key] = self.standing.faults_by_kind.get(key, 0) + 1
        self.standing.last_faults = tuple(fault.explanation for fault in faults)

        if faults:
            self.standing.inconsistent += 1
            return self._verdict(
                segment, INCONSISTENT, tuple(faults),
                f"{len(faults)} contradiction(s): " + "; ".join(f.explanation for f in faults),
            )

        self.standing.consistent += 1
        return self._verdict(
            segment, CONSISTENT, (),
            f"{allotment.allotted:,.2f} allocated, trades between "
            f"{bounds.minimum_capital:,.2f} and {bounds.maximum_capital:,.2f}, "
            f"up to {allotment.leverage_ceiling}x",
        )

    def _verdict(self, segment, verdict, faults, reason) -> CapitalSettingsVerdict:
        return CapitalSettingsVerdict(
            segment=segment, verdict=verdict, faults=faults,
            reason=reason, judged_at_ns=self._now_ns(),
        )


def describe_validation(validator: CapitalSettingsValidator) -> dict:
    return {
        "part_id": PART_ID,
        "judgements": validator.standing.judgements,
        "consistent": validator.standing.consistent,
        "inconsistent": validator.standing.inconsistent,
        "incomplete": validator.standing.incomplete,
        "faults_by_kind": dict(validator.standing.faults_by_kind),
        "last_faults": list(validator.standing.last_faults),
    }


def run_capital_settings_validator(
    validator: CapitalSettingsValidator, control_socket, read_settings, publish_verdicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_verdicts(tuple(validator.judge(**settings) for settings in read_settings()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_validation(validator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The gate before an order is bounded refuses anything this part has not judged
    valid, which makes this the part that decides whether any order is ever placed.
    It judges the segment's own allotment against the operator's main account: an
    allotment larger than the account, or bounds that cross, are the settings
    mistakes that would otherwise be discovered by a position.

    It judges on every tick rather than only when settings change, because a verdict
    is a level: the gate needs to know the settings are valid *now*, and a verdict
    published once would have to be remembered by every part that reads it.
    """
    from runtime.input_assembly import Batch, LatestValue

    allotments = LatestValue(read=context.bus.reader("capital-allotment"))
    accounts = LatestValue(read=context.bus.reader("main-account-setting"))
    bounds = Batch(read=context.bus.reader("trade-capital-bounds"))
    ceilings = Batch(read=context.bus.reader("leverage-ceiling"))
    instruments = LatestValue(read=context.bus.reader("instrument-choice"))
    publish_verdicts = context.bus.publisher_for("capital-settings-verdict")
    segment = str(context.setting("segment_id").value)

    def read_settings():
        bounds.payloads()
        ceilings.payloads()
        allotment = allotments.value()
        account = accounts.value()
        chosen = instruments.value()
        return (
            {
                "segment": segment,
                "allotment": allotment,
                "main_balance": getattr(account, "balance", None),
                "instrument_maximum_leverage": getattr(
                    getattr(chosen, "chosen", None), "maximum_leverage", None
                ),
            },
        )

    return run_capital_settings_validator(
        validator=CapitalSettingsValidator(),
        control_socket=context.control_socket,
        read_settings=read_settings,
        publish_verdicts=publish_verdicts,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
