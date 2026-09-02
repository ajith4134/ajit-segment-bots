"""What the five hard-channel parts actually observed on this machine.

The hard channel is the half of stock-market-news-data a part *refuses* on
rather than weighs: NSE's own F&O ban and ASM lists, its corporate-action file,
and the trading session the clock is really in. This script reads each part's
own standing out of the heartbeat table `heartbeat-collector` writes -- the same
file the boards read, not a second count kept for this script, which would be
free to disagree with the one the parts act on.

Nothing here is asserted. A part that has published no standing prints
NOT MEASURED, which is a different fact from a standing that says zero: the
first is a gap in the evidence and the second is a measurement. A table older
than `heartbeat_silent_after_seconds` proves nothing about any part either, so
it is refused whole rather than read as "all quiet" (Rule 8).

**Two of the facts worth reporting cannot ride the heartbeat at all**, and the
second half of this script exists because of that. The standing channel is
numeric by design (`runtime/part_process.py` `_standing_counters`: anything not
a number is left behind, so a per-symbol map cannot evict every counter a part
has). `corporate-action-adjuster`'s refused *wordings* and
`market-session-calendar`'s session are text, so the table carries
`wordings_refused_count = 2` and never the two wordings, and `holidays_known`
and never the session. Both are re-read here from NSE's own files with the same
classes the parts run -- a second measurement of the same source, clearly
labelled as taken now rather than read off the running parts.

Run:  .venv/bin/python measurements/2026-09-02-hard-channel/what_the_hard_channel_saw.py
"""

import json
import pathlib
import sys
import time

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "dashboard"))

HARD_CHANNEL_PARTS = (
    "trading-restriction-reader",
    "instrument-restriction-state",
    "corporate-action-reader",
    "corporate-action-adjuster",
    "market-session-calendar",
)

NOT_MEASURED = "NOT MEASURED"


def read_the_table() -> tuple[dict | None, str]:
    """The last heartbeat table, or None with the reason there is no reading."""
    from build_part_monitor import read_heartbeat_table_path
    from part_activity import read_silence_threshold_seconds
    from parts.observability.heartbeat_collector import read_heartbeat_table_file

    try:
        table_path = read_heartbeat_table_path()
    except Exception as refusal:
        return None, f"{NOT_MEASURED}: settings refused the table path ({refusal})"

    document = read_heartbeat_table_file(table_path)
    if document is None:
        return None, (
            f"{NOT_MEASURED}: no readable heartbeat table at {table_path} -- "
            "heartbeat-collector has not written one"
        )

    age_seconds = (time.time_ns() - int(document.get("collected_at_ns", 0))) / 1e9
    silent_after = read_silence_threshold_seconds()
    if silent_after is not None and age_seconds >= silent_after:
        return None, (
            f"{NOT_MEASURED}: the table at {table_path} is {age_seconds:.0f}s old, past "
            f"the {silent_after:.0f}s silence threshold -- the collector has stopped"
        )
    return document, f"read {table_path}, collected {age_seconds:.0f}s ago"


def print_what_each_part_saw(document: dict) -> int:
    """One line of proof per part. Returns how many of the five reported."""
    beats = {
        beat.get("part_id", ""): beat for beat in document.get("heartbeats", ())
    }
    reporting = 0
    for part_id in HARD_CHANNEL_PARTS:
        beat = beats.get(part_id)
        if beat is None:
            print(f"{part_id:30} {NOT_MEASURED} -- no heartbeat")
            continue
        reporting += 1
        state = beat.get("state", NOT_MEASURED)
        age = beat.get("age_seconds")
        age_text = "age unknown" if age is None else f"{float(age):.0f}s ago"
        print(f"{part_id:30} {state:14} {age_text}")
        standing = beat.get("standing") or {}
        if not standing:
            print(f"{'':30}   (no standing published)")
            continue
        for name in sorted(standing):
            print(f"{'':30}   {name} = {json.dumps(standing[name])}")
    return reporting


def read_a_setting(name: str) -> str:
    from runtime.settings_reader import load_settings_document, settings_directory

    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return str(document.read_value(name))


def print_the_text_facts_the_heartbeat_cannot_carry() -> None:
    """The session and the refused wordings, re-read from NSE now.

    The same classes the two parts run, against the same URLs, so this is a
    second reading of one source rather than a restatement of the parts' own
    answer -- which is the only honest way to get a text fact off a numeric
    channel. A fetch that fails prints the refusal, never a guess.
    """
    import datetime
    import zoneinfo

    from parts.stock_market_news_data.corporate_action_adjuster import (
        CorporateActionAdjuster,
    )
    from parts.stock_market_news_data.corporate_action_reader import (
        CORPORATE_ACTIONS_URL,
        CorporateActionReader,
    )
    from parts.stock_market_news_data.market_session_calendar import (
        EXCHANGE_TIMEZONE,
        HOLIDAY_URL,
        MarketSessionCalendar,
        read_clock_time,
    )
    from runtime.nse_public_data import NsePublicData, open_browser_session

    print("Re-read from NSE now, because the heartbeat channel is numeric only:")
    try:
        nse = NsePublicData(
            session=open_browser_session(),
            timeout_seconds=float(read_a_setting("nse_public_data_timeout_seconds")),
        )
        timezone = zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
        calendar = MarketSessionCalendar(
            segment=read_a_setting("market_session_segment"),
            opens_at=read_clock_time(read_a_setting("market_session_opens_at_ist")),
            closes_at=read_clock_time(read_a_setting("market_session_closes_at_ist")),
            timezone=timezone,
        )
        calendar.observe_holidays(nse.read_json(HOLIDAY_URL))
        now = datetime.datetime.now(tz=timezone)
        state = calendar.session_at(now)
        print(f"  session at {now:%Y-%m-%d %H:%M} IST: {state.kind} -- {state.reason}")

        reader = CorporateActionReader()
        adjuster = CorporateActionAdjuster()
        rows = nse.read_json(CORPORATE_ACTIONS_URL)
        for report in reader.reports_from(rows, observed_at_ns=time.time_ns()):
            adjuster.action_for(report)
        print(
            f"  corporate actions: {adjuster.understood} understood, "
            f"{adjuster.refused} refused of {reader.rows_seen} rows"
        )
        if not adjuster.wordings_refused:
            print("  no wording was refused")
        for wording in adjuster.wordings_refused:
            print(f"  refused wording: {wording!r}")
    except Exception as refusal:
        print(f"  {NOT_MEASURED}: NSE could not be read now ({refusal!r})")


def main() -> int:
    document, proof = read_the_table()
    print(proof)
    print()
    if document is None:
        return 1
    reporting = print_what_each_part_saw(document)
    print()
    print(f"{reporting} of {len(HARD_CHANNEL_PARTS)} hard-channel parts reported a heartbeat")
    print()
    print_the_text_facts_the_heartbeat_cannot_carry()
    return 0 if reporting == len(HARD_CHANNEL_PARTS) else 1


if __name__ == "__main__":
    sys.exit(main())
