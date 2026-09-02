"""corporate-action-reader: read the exchange's own corporate action file.

`www.nseindia.com/api/corporates-corporateActions?index=equities`, verified live
2026-09-02: a JSON list, each row carrying `symbol`, `series`, `isin`,
`faceVal`, `subject`, `exDate`, `recDate` plus book-closure and no-delivery
windows.

**NSE says "no value" two ways in the same document** -- the string "-" in the
date and faceVal fields, and JSON null in caBroadcastDate. Read as a date the
first is a parse error; defaulted to today it is worse, because an action dated
today is an action that adjusts a price series right now. So a missing optional
value is None, and a row with no ex-date is skipped and counted: the ex-date is
the only thing an adjustment can be applied from, and a row dropped without a
count is a corporate action nobody knows was missed.

This part reads and republishes only. What an action *does* to a price is
corporate-action-adjuster's job, because reading NSE's file and interpreting
"Bonus 1:1" are two responsibilities (T-6).
"""

from __future__ import annotations

from runtime.market_conditions import CorporateActionReport, read_nse_date
from runtime.nse_public_data import NSE_API_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "corporate-action-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("corporate-action-report", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CORPORATE_ACTIONS_URL = f"{NSE_API_HOST}/api/corporates-corporateActions?index=equities"

# NSE's own placeholder for "no value", in every field it uses one.
NOT_STATED = "-"


def _is_stated(value) -> bool:
    return value is not None and str(value).strip() != NOT_STATED


def _optional_date(stated):
    if not _is_stated(stated):
        return None
    return read_nse_date(stated)


def _optional_number(stated):
    if not _is_stated(stated):
        return None
    return float(stated)


class CorporateActionReader:
    """Turns NSE's corporate action rows into reports."""

    def __init__(self) -> None:
        self._rows_seen = 0
        self._rows_without_an_ex_date = 0

    def reports_from(self, rows, observed_at_ns: int) -> tuple:
        reports = []
        for row in rows:
            self._rows_seen += 1
            ex_date = _optional_date(row.get("exDate"))
            if ex_date is None:
                self._rows_without_an_ex_date += 1
                continue
            reports.append(CorporateActionReport(
                symbol=row["symbol"], series=row.get("series") or "",
                isin=row.get("isin") or "", subject=row.get("subject") or "",
                ex_date=ex_date, record_date=_optional_date(row.get("recDate")),
                face_value=_optional_number(row.get("faceVal")),
                observed_at_ns=observed_at_ns,
            ))
        return tuple(reports)

    @property
    def rows_seen(self) -> int:
        return self._rows_seen

    @property
    def rows_without_an_ex_date(self) -> int:
        return self._rows_without_an_ex_date


def describe_reader(reader: CorporateActionReader) -> dict:
    return {
        "part_id": PART_ID,
        "rows_seen": reader.rows_seen,
        "rows_without_an_ex_date": reader.rows_without_an_ex_date,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    reader = CorporateActionReader()
    publish_reports = context.bus.publisher_for("corporate-action-report")
    poll_seconds = context.number("corporate_action_poll_seconds")
    last_polled_at: list[float | None] = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is not None and now - last_polled_at[0] < poll_seconds:
            return
        last_polled_at[0] = now
        reports = reader.reports_from(nse.read_json(CORPORATE_ACTIONS_URL), time.time_ns())
        if reports:
            publish_reports(reports)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_reader(reader) | nse.standing(),
    )


__all__ = [
    "CORPORATE_ACTIONS_URL",
    "CorporateActionReader",
    "PART_DECLARATION",
    "PART_ID",
    "describe_reader",
    "start_part",
]
