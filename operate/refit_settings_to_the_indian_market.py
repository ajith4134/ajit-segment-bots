"""Re-fit settings whose numbers were measured on crypto to the Indian market.

Goal 2, item 3 -- the acting half. A file that mentions Binance is a naming
problem; a **number** fitted to Binance is a decision being made every tick on a
market this project does not trade. `mean_reversion_minimum_volatility_fraction`
was five basis points because that was "just under the round trip at the venues'
taker fees"; on NSE it refused 92.2% of every in-session NIFTY observation and
the detector raised zero candidates.

Every value below is derived from real Indian data and carries where it came
from, per RL-061. The measurement is in
`measurements/2026-09-12-indian-order-sizes/`, taken from this project's own
captured tape for 2026-09-08 -- 601 instruments, 7,822 share prints and 68,791
option prints:

    kind     median      p75       p95       p99       max
    share     2,224    8,572    52,866   183,990   1,093,495
    option   13,722   44,026   201,000   464,100     969,000

**Idempotent.** A setting already carrying the Indian value is left alone and
reported as such. Re-running changes nothing.

**Writes the operator's live file**, so it backs it up first and refuses while
it cannot. Settings are the operator's (RL-055); this exists so the change is
reviewable as a script with its reasoning attached, not so it happens quietly.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time

SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"

MEASURED = "measurements/2026-09-12-indian-order-sizes/"
TAPE = "this project's own captured tape for 2026-09-08"

# What this desk actually trades, from the segment files: a trade commits between
# 100,000 and 200,000 rupees. A reference order size exists to measure book
# impact *for the orders this bot places*, so the size it trades is the honest
# basis -- not a multiple of the median print, which is what the crypto figure
# used because a crypto bot's order was near the median print.
DESK_ORDER_SIZE = 150_000.0

REFITS: dict[str, tuple[str, str]] = {
    # (new value as it should appear after `value = `, the provenance sentence)
    "captured_venues": (
        "[]",
        "Claude, 2026-09-12: the venues captured through a VENUE ADAPTER "
        '(runtime/venues/*.py). Was ["binance-usdm", "bybit-linear"], markets '
        "this project has not traded since the pivot of 2026-09-01. EMPTY, not "
        '["upstox"], and the difference is the point: Upstox is reached through '
        "a BROKER adapter (runtime/brokers/upstox.py), which is a different "
        "interface, so naming it here made `load_venue_adapter` look for "
        "runtime/venues/upstox.py and crash-loop feed-gap-detector -- measured "
        "on this spine the moment it was tried. This spine captures no "
        "venue-adapter venue at all, and empty is how that is said. The broker "
        "feed is watched through `broker_feed_venue_id`, which feed-gap-detector "
        "already adds separately (2026-09-06, added because every Upstox print "
        "was otherwise skipped and the part watched two streams that had already "
        "stopped). symbol-catalogue-reader and stream-budget-planner refuse an "
        "empty list by design -- correctly, since they are the crypto capture "
        "path and neither is on this spine; broker-symbol-universe-bridge "
        "produces `symbol-universe` in their place.",
    ),
    "market_thesis_bellwether_symbols": (
        '["NIFTY", "BANKNIFTY", "SENSEX"]',
        'Claude, 2026-09-12: the symbols a market thesis is formed against. Was '
        '["BTCUSDT", "ETHUSDT", "SOLUSDT"]. A bellwether has to be something '
        "this market actually watches, and these are measurably the three "
        f"busiest indices on {TAPE}: NIFTY 30,269 prints, BANKNIFTY 24,187, "
        "SENSEX 5,831, with no other index reaching a thousand. NIFTY is the "
        "market, BANKNIFTY its largest sector, SENSEX the other exchange's "
        "benchmark. Ranked by captured activity rather than by index weight, "
        "which this project does not hold constituent weights for -- so it is a "
        "liquidity ranking and says so.",
    ),
    "bull_reference_order_size_quote": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: the order size the bull bot measures book impact "
        "against. Was 1000.0 USDT, chosen as above the 166 USDT median trade on "
        "binance-usdm. The rupee figure is derived differently and deliberately: "
        "this desk's own trade commits between minimum_capital_per_trade "
        "(100,000) and maximum_capital_per_trade (200,000), so 150,000 is the "
        "size it really places, and impact should be measured at the size that "
        f"will be traded. Measured against {TAPE}, that is about 11x the median "
        "option print (13,722) and above the 95th percentile (201,000 is p95), "
        "which correctly says this desk is a large participant per order rather "
        f"than a typical one. Distribution in {MEASURED}.",
    ),
    "bear_reference_order_size_quote": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: mirrored for the short side from "
        "bull_reference_order_size_quote, same derivation and same measurement. "
        "Was 1000.0 USDT.",
    ),
    "liquidity_reference_order_size": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: the order size liquidity-grader walks the book at. "
        "Was 1000.0 USDT. Set to the same 150,000 rupees the two bots measure "
        "impact against, because a grade taken at a different size than the "
        "order that will be placed is a grade for an order nobody sends -- which "
        "is the mismatch runtime/symbol_round_trip_cost.py exists to close.",
    ),
    "slippage_size_bands": (
        '"10000,50000,200000"',
        "Claude, 2026-09-12: the notional bands slippage is learned per. Was "
        '"1000,10000,100000" USDT. Set to the real shape of Indian option '
        f"prints on {TAPE}: median 13,722, p75 44,026, p95 201,000. The bands "
        "bracket that distribution so each holds a real population rather than "
        "one holding everything -- the crypto bands in rupees would have put "
        "almost every print in the top band and learned one number.",
    ),
    "anomaly_minimum_quote_volume_for_a_move": (
        "10000.0",
        "Claude, 2026-09-12: the traded value a price move needs before it "
        "counts as a move rather than a print. Was 1000.0 USDT. Ten thousand "
        f"rupees sits just below the median option print on {TAPE} (13,722) and "
        "well above the median share print (2,224), so a move carrying less than "
        "this really is a small print -- which is the question the setting asks.",
    ),
    "whale_minimum_quote_value": (
        "1000000.0",
        "Claude, 2026-09-12: the traded value above which one print is somebody "
        "large rather than ordinary flow. The NUMBER is unchanged from the "
        "crypto era and the MEANING is not: 1,000,000 USDT was roughly eight and "
        "a half crore rupees, which no single NSE print reaches. Measured on "
        f"{TAPE}, the largest single print seen at all was 1,093,495 rupees on a "
        "share and 969,000 on an option, so one million rupees is the top of the "
        "observed distribution and is the right place for this line. Kept at the "
        "same digits by coincidence of unit, which is exactly why the provenance "
        "has to say so rather than leaving it looking untouched.",
    ),
    "autonomy_notional_ceilings": (
        "[0.0, 0.0, 100000.0, 200000.0]",
        "Claude, 2026-09-12: the most notional each autonomy tier may commit. "
        "Was [0.0, 0.0, 1000.0, 5000.0] USDT. Re-expressed against this desk's "
        "own bounds rather than converted at a rate: the two open tiers are now "
        "minimum_capital_per_trade (100,000) and maximum_capital_per_trade "
        "(200,000), so an autonomous tier can never commit more than the "
        "operator already allows a single trade. The two zero tiers stay zero -- "
        "they are the tiers that may not trade at all, and that is not a "
        "currency question.",
    ),
    "policy_earned_size_per_competence": (
        "100000.0",
        "Claude, 2026-09-12: how much notional one unit of demonstrated "
        "competence earns. Was 1000.0 USDT. Set to minimum_capital_per_trade, "
        "so the first competence a bot earns buys exactly one tradeable trade on "
        "this desk rather than a fraction of one -- at the old figure, converted "
        "or not, an earned unit bought a size trade-capital-bounds-gate would "
        "refuse as below the minimum.",
    ),
}


def back_up(path: pathlib.Path) -> pathlib.Path:
    destination = path.with_name(
        f"{path.name}.before-indian-refit-{time.strftime('%Y-%m-%dT%H%M%S')}"
    )
    shutil.copy2(path, destination)
    return destination


def refit(text: str, name: str, new_value: str, note: str) -> tuple[str, str]:
    """Replace one setting's value and append its provenance. Returns (text, what happened)."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]

    current = re.search(r"^value\s*=\s*(.+)$", block, re.M)
    if current is None:
        return text, "NO VALUE LINE"
    if current.group(1).strip() == new_value.strip():
        return text, "already Indian"

    was = current.group(1).strip()
    updated = block[:current.start(1)] + new_value + block[current.end(1):]

    marker = re.search(r'note\s*=\s*"(.*)"', updated, re.S)
    if marker is not None:
        addition = f" REFITTED 2026-09-12, was {was}. {note}"
        updated = updated[:marker.end(1)] + addition.replace('"', "'") + updated[marker.end(1):]
    return text[:start] + updated + text[(end if end > 0 else len(text)):], f"{was} -> {new_value}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write. Without it, report only.")
    arguments = parser.parse_args()

    if not SETTINGS.exists():
        print(f"no settings at {SETTINGS}", file=sys.stderr)
        return 2
    text = SETTINGS.read_text()
    if arguments.apply:
        print(f"backed up to {back_up(SETTINGS)}")
    else:
        print("DRY RUN -- nothing will be written. Pass --apply to do it.")

    changed = 0
    for name, (value, note) in REFITS.items():
        text, what = refit(text, name, value, note)
        print(f"  {name:46} {what}")
        if "->" in what:
            changed += 1
    if arguments.apply and changed:
        # **Parse before writing.** A provenance note is prose going into a TOML
        # string, and prose contains quotes and backslashes. An ad-hoc edit to
        # one of these notes on 2026-09-12 put `\'` into a basic string, TOML
        # refused the whole file, and the spine could not start at all -- every
        # part down, because of a comment. The settings file is the one input
        # every part reads, so a writer that cannot prove its output parses has
        # no business writing it.
        import tomllib

        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as failure:
            print(
                f"REFUSED: the result would not be valid TOML ({failure}). "
                f"Nothing written; the settings file is untouched.",
                file=sys.stderr,
            )
            return 2
        SETTINGS.write_text(text)
    print(f"\n{changed} setting(s) {'refitted' if arguments.apply else 'would be refitted'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
