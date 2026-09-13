"""Whether NSE is in session, as `market-session-calendar` on the live spine says.

One reader for two askers that must not disagree with the bot: the capture
board's tape-freshness tile and `operate/replay_a_captured_session.py`. Both used
to answer the question for themselves -- the tile with a sixty-second bound fitted
to a 24-hour crypto market, the replay with a calendar that never read its holiday
list and so answered "closed" at every moment. The live spine's answer is the
calendar part's own standing in the heartbeat table (`is_open`,
`has_a_holiday_list`), so that is what this reads.

Substrate, not a part: it reads a file the heartbeat collector writes, and no part
imports it.
"""

from __future__ import annotations

import json
import pathlib
import time

from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

SESSION_CALENDAR_PART = "market-session-calendar"
IN_SESSION = "in session"
OUT_OF_SESSION = "out of session"
NANOSECONDS_PER_SECOND = 1_000_000_000


def read_the_calendars_live_answer() -> tuple[str | None, str]:
    """`market-session-calendar`'s own answer, read from the heartbeat table, and its proof.

    None when there is no answer to trust: settings or table unreadable, the table
    older than `heartbeat_silent_after_seconds`, the calendar not reporting, or no
    holiday list read yet -- in which last case the calendar says CLOSED, and that
    CLOSED is an absence of measurement rather than a closed market.
    """
    settings_path = settings_directory() / "runtime.toml"
    try:
        document = load_settings_document(settings_path, "runtime")
        table_path = pathlib.Path(str(document.read_value("heartbeat_table_path"))).expanduser()
        silent_after = float(document.read_value("heartbeat_silent_after_seconds"))
        table = json.loads(table_path.read_text())
    except (SettingsParseRefused, KeyError, OSError, ValueError) as failure:
        return None, f"no session answer: {type(failure).__name__}: {failure}"

    age = (time.time_ns() - int(table.get("collected_at_ns", 0))) / NANOSECONDS_PER_SECOND
    if age > silent_after:
        return None, f"no session answer: {table_path} is {age:.0f}s old, past {silent_after:.0f}s"
    row = next(
        (row for row in table.get("heartbeats", []) if row.get("part_id") == SESSION_CALENDAR_PART),
        None,
    )
    if row is None or row.get("state") != "reporting":
        return None, f"no session answer: {SESSION_CALENDAR_PART} is not reporting in {table_path}"
    standing = row.get("standing") or {}
    if "is_open" not in standing or not standing.get("has_a_holiday_list"):
        return None, f"no session answer: {SESSION_CALENDAR_PART} has not read its holiday list"
    answer = IN_SESSION if standing["is_open"] else OUT_OF_SESSION
    return answer, f"{SESSION_CALENDAR_PART} standing in {table_path}"
