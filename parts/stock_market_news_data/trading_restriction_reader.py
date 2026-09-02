"""trading-restriction-reader: read the exchange's F&O ban, ASM, GSM, halt lists.

Two NSE endpoints, both public, both verified live 2026-09-02:

- `nsearchives.nseindia.com/content/fo/fo_secban.csv` -- not really a CSV. Line
  one is a sentence carrying the trade date it is stated for
  ("Securities in Ban For Trade Date 02-SEP-2026:"); each later line is
  `<serial>,<symbol>`. Read with `csv` alone the header becomes a banned
  instrument named after the sentence, and skipped blindly it loses the only
  date the file states.
- `www.nseindia.com/api/reportASM` -- `{"longterm": {"data": [...]},
  "shortterm": {"data": [...]}}`, each row carrying `symbol`, `survDesc` and
  `asmTime`. The two lists are different restrictions and are kept apart.

**An empty ban list is a real answer.** Most days ban nothing, so zero reports
must mean zero -- which is exactly why `NsePublicData` raises on a non-200
instead of handing this part an error page to find no symbols in.

Whether an ASM stage is reported at all is a setting, not a rule of the
exchange: NSE's ASM raises margin rather than forbidding trade, and Phase A
treats it as blocking only because no margin model exists yet
(`asm_restricts_new_positions`).
"""

from __future__ import annotations

import re

from runtime.market_conditions import (
    InstrumentRestrictionReport,
    RestrictionKind,
    read_nse_date,
)
from runtime.nse_public_data import NSE_API_HOST, NSE_ARCHIVE_HOST
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trading-restriction-reader"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("instrument-restriction-report", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

BAN_FILE_URL = f"{NSE_ARCHIVE_HOST}/content/fo/fo_secban.csv"
ASM_URL = f"{NSE_API_HOST}/api/reportASM"

BAN_SOURCE = "nse-fo-secban"
ASM_LONG_TERM_SOURCE = "nse-asm-longterm"
ASM_SHORT_TERM_SOURCE = "nse-asm-shortterm"

# "Securities in Ban For Trade Date 02-SEP-2026:" -- the one date the file states.
BAN_HEADER_DATE = re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4})")

ASM_LIST_KINDS = {
    "longterm": (RestrictionKind.ASM_LONG_TERM, ASM_LONG_TERM_SOURCE),
    "shortterm": (RestrictionKind.ASM_SHORT_TERM, ASM_SHORT_TERM_SOURCE),
}


class TradingRestrictionReader:
    """Turns NSE's own two restriction publications into reports."""

    def __init__(self) -> None:
        self._banned_symbols_last_seen = 0
        self._asm_symbols_last_seen = 0

    def reports_from_ban_file(self, body: str, observed_at_ns: int) -> tuple:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            self._banned_symbols_last_seen = 0
            return ()
        header = lines[0]
        stated = BAN_HEADER_DATE.search(header)
        if stated is None:
            raise ValueError(
                f"the ban file's first line states no trade date: {header!r}. Refusing "
                f"rather than dating today's ban list by wall clock."
            )
        stated_for = read_nse_date(stated.group(1))
        reports = []
        for line in lines[1:]:
            _, _, symbol = line.partition(",")
            symbol = symbol.strip()
            if not symbol:
                continue
            reports.append(InstrumentRestrictionReport(
                symbol=symbol, kind=RestrictionKind.FNO_BAN, source=BAN_SOURCE,
                stated_for=stated_for, detail=header, observed_at_ns=observed_at_ns,
            ))
        self._banned_symbols_last_seen = len(reports)
        return tuple(reports)

    def reports_from_asm(self, document, observed_at_ns: int) -> tuple:
        reports = []
        for list_name, (kind, source) in ASM_LIST_KINDS.items():
            rows = document.get(list_name, {}).get("data", [])
            for row in rows:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                reports.append(InstrumentRestrictionReport(
                    symbol=symbol, kind=kind, source=source,
                    stated_for=read_nse_date(row["asmTime"]),
                    detail=row.get("survDesc", ""), observed_at_ns=observed_at_ns,
                ))
        self._asm_symbols_last_seen = len(reports)
        return tuple(reports)

    @property
    def banned_symbols_last_seen(self) -> int:
        return self._banned_symbols_last_seen

    @property
    def asm_symbols_last_seen(self) -> int:
        return self._asm_symbols_last_seen


def describe_reader(reader: TradingRestrictionReader) -> dict:
    return {
        "part_id": PART_ID,
        "banned_symbols_last_seen": reader.banned_symbols_last_seen,
        "asm_symbols_last_seen": reader.asm_symbols_last_seen,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.nse_public_data import NsePublicData, open_browser_session

    nse = NsePublicData(
        session=open_browser_session(),
        timeout_seconds=context.number("nse_public_data_timeout_seconds"),
    )
    reader = TradingRestrictionReader()
    publish_reports = context.bus.publisher_for("instrument-restriction-report")
    poll_seconds = context.number("trading_restriction_poll_seconds")
    reports_asm = bool(context.setting("asm_restricts_new_positions").value)
    last_polled_at: list[float | None] = [None]

    def tick() -> None:
        now = time.monotonic()
        if last_polled_at[0] is not None and now - last_polled_at[0] < poll_seconds:
            return
        last_polled_at[0] = now
        observed_at_ns = time.time_ns()
        reports = reader.reports_from_ban_file(nse.read_text(BAN_FILE_URL), observed_at_ns)
        if reports_asm:
            reports += reader.reports_from_asm(nse.read_json(ASM_URL), observed_at_ns)
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
    "ASM_URL",
    "BAN_FILE_URL",
    "PART_DECLARATION",
    "PART_ID",
    "TradingRestrictionReader",
    "describe_reader",
    "start_part",
]
