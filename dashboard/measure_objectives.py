#!/usr/bin/env python3
"""What this project is trying to do, and how far it has got — measured, not recalled.

    python3 dashboard/measure_objectives.py

Both temporary goals in `docs/goal.md` are long-running, and the second one is
explicitly multi-session. A goal held only in a session's head is a goal that
drifts: on 2026-09-06 a whole investigation went into *tuning* a crypto
integration test — working out which Binance symbols happened to cointegrate —
when the standing instruction was to **replace** it with the Indian-market
equivalent. Nothing said otherwise, because nothing was measuring it. The
operator asked for a measurement so it cannot happen again.

So this reports the objectives as numbers:

    1. the three segment bots, and whether any has traded live yet
    2. the 29-feature audit, and how much of the diagram carries
    3. what is still shaped like crypto, which is the thing goal 2 item 2 asks for

Every figure here is read from a file on this machine or from the blueprint.
Nothing is asserted, and anything unmeasured says so rather than reading as
healthy (Rule 8).
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
POSITIONS = STATE / "positions"
LEDGER = PROJECT / "docs/feature-audit.md"

# What "still crypto" looks like in this codebase's own words. Deliberately the
# venue names and the settlement currency rather than the word "crypto": a file
# that merely mentions the retirement is not itself crypto-shaped, while one that
# names Binance is.
CRYPTO_MARKERS = ("binance", "bybit", "ccxt", "USDT")

# Files that are legitimately about crypto and are not drift: the venue adapters
# themselves, the tape they wrote, and this measurement. Retiring them is a
# separate decision from converting the parts that still reason in their terms.
# Files that must name a crypto venue to do their job, and whose naming is
# therefore not drift. Kept deliberately short: every addition here makes the
# number fall without anything being fixed, which is the one way this guard can
# lie. A file belongs here only when naming crypto IS its purpose.
LEGITIMATELY_CRYPTO = (
    "runtime/venues/binance_usdm.py",
    "runtime/venues/bybit_linear.py",
    "runtime/venues/adapter_registry.py",
    "dashboard/measure_objectives.py",
    # The tool that finds and retires crypto-fitted learned state. Same argument
    # as this file: a detector that may not name what it detects cannot work.
    "operate/retire_crypto_learned_state.py",
)

# Files that still name crypto and are NOT excused, listed so the remainder is
# legible rather than a bare count. Every one is the crypto venue path, kept
# rather than deleted because the operator's instruction is convert or replace
# -- and none has an Indian equivalent built yet:
#
#   parts/execution_venue_adapter/ccxt_order_router.py
#   parts/execution_venue_adapter/venue_order_status_translator.py
#   parts/market_data_feed/ccxt_venue_reader.py
#       the real-money crypto execution and feed path, correctly off for paper
#       trading. Replacing them means building the Upstox order router.
#   parts/intelligence/market_event_reader.py
#       its crypto symbol pattern is now only a fallback for a venue that
#       publishes free text and resolves nothing; the Indian path reads the
#       symbols exchange-announcement-reader resolved against the universe.
#
# They stay counted on purpose. The number should read as work outstanding.

SEGMENTS = ("index-options", "stock-options", "cash-equity-intraday")


def paper_account_of(segment: str) -> dict | None:
    """One segment's paper account, as the keeper last checkpointed it."""
    path = POSITIONS / f"paper-account-keeper.paper-account-{segment}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["state"]
    except (OSError, ValueError, KeyError):
        return None


def features_walked() -> tuple[int, int]:
    """How many of the 29 the ledger records as walked, and how many there are."""
    if not LEDGER.exists():
        return (0, 0)
    text = LEDGER.read_text(encoding="utf-8")
    walked = len(re.findall(r"\|\s*\*\*walked \d{4}-\d{2}-\d{2}\*\*", text))
    total = len(re.findall(r"^\|\s*\d+\s*\|\s*`", text, flags=re.MULTILINE))
    return (walked, total)


def names_a_crypto_venue_in_code(source: pathlib.Path) -> bool:
    """Whether this file still *uses* a crypto venue, rather than mentioning one.

    The distinction is the whole value of the number. A file ported to the
    Upstox tape keeps a comment saying what it was ported from -- that is
    history and belongs there. A file that still hands "binance-usdm" to an
    adapter is drift. Counting both together produces a figure that never falls
    however much is converted, and a guard that cannot tell the two apart is a
    guard nobody will act on.

    So docstrings and comments are excluded and the rest of the syntax tree is
    searched: string constants that are not docstrings, and identifiers.
    """
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if any(marker.lower() in node.value.lower() for marker in CRYPTO_MARKERS):
                return True
        if isinstance(node, ast.Name) and any(
            marker.lower() in node.id.lower() for marker in CRYPTO_MARKERS
        ):
            return True
        if isinstance(node, ast.Attribute) and any(
            marker.lower() in node.attr.lower() for marker in CRYPTO_MARKERS
        ):
            return True
    return False


def files_that_still_name_a_crypto_venue() -> dict[str, list[str]]:
    """Every source file whose *code* still names a crypto venue, by area."""
    found: dict[str, list[str]] = {}
    for area in ("parts", "runtime", "tests", "operate", "dashboard"):
        directory = PROJECT / area
        if not directory.is_dir():
            continue
        paths = []
        for source in sorted(directory.rglob("*.py")):
            relative = str(source.relative_to(PROJECT))
            if relative in LEGITIMATELY_CRYPTO:
                continue
            if names_a_crypto_venue_in_code(source):
                paths.append(relative)
        if paths:
            found[area] = paths
    return found


def coverage() -> dict | None:
    """The RL-072 figures, from the probe that measures them."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    try:
        from measure_diagram_coverage import measure_diagram_coverage

        return measure_diagram_coverage().as_dict()
    except Exception:
        return None


# A setting's `note` is where RL-061 requires its provenance. A note that
# justifies a number by naming a crypto venue, a perpetual, or a taker fee is a
# number fitted to a market this project no longer trades -- and unlike a file
# that merely mentions Binance, such a number is silently *acting* every tick.
# Word boundaries matter here: an unanchored search for "eth" or "sol" matches
# "method" and "resolution" and reports 176 where the truth is 82.
CRYPTO_PROVENANCE = re.compile(
    r"\b(binance|bybit|[A-Z]{2,10}USDT|USDT|BTC|ETH|SOL|perpetuals?|crypto"
    r"|the venues'|taker fee|funding rate)\b"
)


def settings_whose_provenance_is_crypto() -> tuple[int, int, int]:
    """(fitted to crypto, of those read by running code, settings in total).

    The second figure is the one that matters: a crypto-derived number nobody
    reads is dead weight, and a crypto-derived number a detector reads every
    tick is what stopped `mean-reversion-detector` raising a single candidate
    on real NIFTY prices until 2026-09-06.
    """
    path = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"
    try:
        text = path.read_text()
    except OSError:
        return (0, 0, 0)
    total = 0
    fitted: list[str] = []
    for block in re.split(r"\n(?=\[)", text):
        named = re.match(r"\[([a-z0-9_]+)\]", block)
        if named is None:
            continue
        total += 1
        if CRYPTO_PROVENANCE.search(block):
            fitted.append(named.group(1))
    # Named directories rather than a glob pattern: `[pr][ao][rn]*` looks like it
    # covers parts and runtime and silently covers only parts, because "u" is not
    # in [ao]. A search that quietly halves its own scope reports a falling number
    # as progress, which is the exact failure this whole probe exists to prevent.
    sources = [
        source
        for directory in ("parts", "runtime")
        for source in (PROJECT / directory).rglob("*.py")
    ]
    read_by_code = 0
    for name in fitted:
        quoted = f'"{name}"'
        for source in sources:
            try:
                if quoted in source.read_text(encoding="utf-8", errors="ignore"):
                    read_by_code += 1
                    break
            except OSError:
                continue
    return (len(fitted), read_by_code, total)


PART_PURPOSE_LEDGER = PROJECT / "docs/part-purpose-audit.md"

# The third temporary goal's own measurement (2026-09-06). Deliberately counts
# the rows still reading NOT MEASURED rather than the ones judged: this goal
# exists because a part can be RUNNING with every wire CARRYING and still
# publish decoration, so the number that matters is how much of the system
# nobody has actually looked at.
PART_JUDGED = "SERVING ITS PURPOSE"
PART_SKELETON = "SKELETON"
PART_UNJUDGED = "NOT MEASURED"


# What a crypto venue, symbol or feature looks like inside a learned checkpoint.
# Wider than CRYPTO_MARKERS on purpose: a model does not name its venue, it names
# the features it was fitted on, and `funding_rate` is a perpetual's cost of
# carry that no NSE option has.
CRYPTO_IN_LEARNED_STATE = re.compile(
    r"binance|bybit|USDT|USDC|funding_rate|funding_forecast", re.IGNORECASE
)


def learned_state_still_fitted_to_crypto() -> tuple[int, int, list[tuple[str, int, int]]]:
    """(checkpoints carrying crypto, checkpoints found, per-file crypto/total keys).

    **The category this probe was blind to until 2026-09-12.** Sections 3 and 4
    count source files and settings; neither can see a model's own saved state,
    and that state is what the bot actually acts on. `bull-conviction-model`
    restores 98,862 labels every start, of which `funding_rate` has 44,812
    observations and is its third-largest weight, while the Indian features that
    replaced it -- `open_interest_change`, `order_flow_imbalance` -- have 65 and
    80. `signal-excursion-profiler` restores 1,049 symbol keys of which 746 are
    Binance and Bybit.

    A converted feature builder stops *feeding* a crypto feature. It does not
    unlearn one: the surviving weights were co-fitted with it and the
    normalisation moments are on crypto scales, so a model carrying that state
    is not merely holding dead weight, it is deciding with a function fitted to
    a market this project does not trade.

    Counted per checkpoint as (crypto-named keys, total keys) where the state is
    keyed by symbol, so a file that is being converted shows the ratio falling
    rather than staying binary.
    """
    root = pathlib.Path.home() / ".local/share/ajit-segment-bots/learned"
    if not root.is_dir():
        return (0, 0, [])
    rows: list[tuple[str, int, int]] = []
    carrying = 0
    found = 0
    for checkpoint in sorted(root.glob("*.json")):
        found += 1
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8")).get("state", {})
        except (OSError, ValueError):
            continue
        keys = _symbol_keys_of(state)
        crypto_keys = [key for key in keys if CRYPTO_IN_LEARNED_STATE.search(key)]
        names_crypto = bool(CRYPTO_IN_LEARNED_STATE.search(json.dumps(state)))
        if names_crypto:
            carrying += 1
            rows.append((checkpoint.name, len(crypto_keys), len(keys)))
    return (carrying, found, rows)


def _symbol_keys_of(state) -> list[str]:
    """Every dictionary key in a learned state that looks like a per-symbol one.

    Read structurally rather than by a fixed path: each part checkpoints its own
    shape, and a probe that knew one part's layout would silently measure
    nothing for the rest.
    """
    keys: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and "|" in key:
                    keys.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(state)
    return keys


def parts_judged_for_purpose() -> tuple[int, int, int]:
    """(serving their purpose, found to be skeletons, parts in the ledger).

    A row counts only when its first cell is a part id the blueprint actually
    declares. Matching on the table shape instead counted the legend at the top
    of the ledger -- which names each verdict in a cell of its own -- and read
    376 parts with 2 already judged on the day the ledger was created empty.
    A probe that miscounts in the optimistic direction is the failure Rule 8 is
    about, so the blueprint decides what a part row is.
    """
    try:
        text = PART_PURPOSE_LEDGER.read_text()
    except OSError:
        return (0, 0, 0)
    try:
        blueprint = json.loads((PROJECT / "docs/features.json").read_text())
        part_ids = {feature["id"] for feature in blueprint["features"]}
    except (OSError, ValueError, KeyError):
        return (0, 0, 0)

    serving = skeleton = counted = 0
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or cells[1].strip("`") not in part_ids:
            continue
        counted += 1
        verdict = cells[2]
        if verdict == PART_JUDGED:
            serving += 1
        elif verdict == PART_SKELETON:
            skeleton += 1
    return (serving, skeleton, counted)


def main() -> int:
    print("What this project is trying to do, measured\n")

    print("1. THREE SEGMENT BOTS, PAPER TRADING ON LIVE DATA  (docs/goal.md, first)")
    for segment in SEGMENTS:
        account = paper_account_of(segment)
        if account is None:
            print(f"     {segment:<24} NOT MEASURED  no paper account checkpoint on disk")
            continue
        fills = account.get("fills_applied", 0)
        state = "has traded" if fills else "NOTHING YET"
        print(
            f"     {segment:<24} {state:<12} fills_applied {fills}, "
            f"cash {account.get('cash', 0):,.0f} of {account.get('starting', 0):,.0f}"
        )
    print("     A live paper trade is what this goal asks for. Replay is not it (RL-071).\n")

    print("2. THE 29-FEATURE AUDIT  (docs/goal.md, second — active every session)")
    walked, total = features_walked()
    if total:
        print(f"     features walked          {walked} of {total}   (docs/feature-audit.md)")
    else:
        print("     features walked          NOT MEASURED  no ledger at docs/feature-audit.md")
    measured = coverage()
    if measured:
        print(
            f"     parts running            {measured['running_parts']} of "
            f"{measured['declared_parts']}"
        )
        print(
            f"     wires carrying           {measured['live_wires']} of "
            f"{measured['declared_wires']}"
            f"   ({measured['live_wires'] / measured['declared_wires']:.1%})"
        )
    else:
        print("     parts and wires          NOT MEASURED  the coverage probe did not run")
    print()

    print("3. WHAT IS STILL SHAPED LIKE CRYPTO  (goal 2, item 2)")
    still = files_that_still_name_a_crypto_venue()
    if not still:
        print("     nothing names a crypto venue outside the retired adapters")
    for area, paths in sorted(still.items()):
        print(f"     {area:<10} {len(paths):>4} file(s) still name {', '.join(CRYPTO_MARKERS)}")
    print(f"     {'total':<10} {sum(len(p) for p in still.values()):>4}")
    print(
        "\n     This number is the drift guard. It went unmeasured until 2026-09-06,\n"
        "     when a session spent its effort working out which Binance symbols\n"
        "     cointegrated instead of porting the test onto the Upstox tape. The\n"
        "     instruction was convert or replace; nothing was counting, so nothing\n"
        "     objected. Run this before deciding what to work on."
    )
    print()

    print("4. NUMBERS STILL FITTED TO CRYPTO  (goal 2, item 3 — the acting half)")
    fitted, read_by_code, total = settings_whose_provenance_is_crypto()
    if total == 0:
        print("     NOT MEASURED  no runtime.toml at the settings path")
    else:
        print("     settings whose provenance names a crypto venue, instrument or fee")
        print(f"     {'fitted to crypto':<26} {fitted:>4} of {total}")
        print(f"     {'of those, read by code':<26} {read_by_code:>4}   these act every tick")
        print(
            "     This overcounts by design: a note that explains why a number is no\n"
            "     longer crypto-derived still names crypto, and telling that apart from\n"
            "     a note that justifies a value by naming Binance is not reliable. It\n"
            "     fails towards reporting drift that is already fixed, never towards\n"
            "     missing drift that is not."
        )
        print(
            "\n     A file that mentions Binance is a naming problem. A *number* fitted\n"
            "     to Binance is a decision being made on a market this project does not\n"
            "     trade. mean_reversion_minimum_volatility_fraction was five basis\n"
            "     points because that was 'just under the round trip at the venues'\n"
            "     taker fees'; on NSE it refused 92.2% of every in-session NIFTY print and\n"
            "     the detector raised zero candidates in-session. Re-derived from\n"
            "     Indian data it is 0.000037, and the same session produced 1,470.\n"
            "     Each of these is a number that has never been checked against the\n"
            "     market it now decides in."
        )
    print()

    print("5. WHAT THE BOT HAS ALREADY LEARNED, AND ON WHICH MARKET  (goal 2)")
    carrying, found, rows = learned_state_still_fitted_to_crypto()
    if found == 0:
        print("     NOT MEASURED  no learned checkpoints on this machine yet")
    else:
        print(f"     {'checkpoints carrying crypto':<32} {carrying:>4} of {found}")
        for name, crypto_keys, total_keys in rows:
            share = f"{crypto_keys}/{total_keys} symbol keys" if total_keys else "feature names only"
            print(f"       {name:<46} {share}")
        print(
            "\n     Sections 3 and 4 cannot see this. They count source files and\n"
            "     settings; a model's own saved state is neither, and it is what the\n"
            "     bot actually decides with. Converting a feature builder stops it\n"
            "     FEEDING a crypto feature -- it does not unlearn one. The surviving\n"
            "     weights were co-fitted with it and the normalisation moments are on\n"
            "     crypto scales, so the decision function is still the crypto one.\n"
            "     Added 2026-09-12, after a session reported the conversion as done\n"
            "     while bull-conviction-model was restoring 44,812 funding_rate\n"
            "     observations every start against 65 for the feature that replaced it."
        )
    print()

    print("6. IS EACH PART ACTUALLY SERVING ITS PURPOSE?  (goal 3, 2026-09-06)")
    serving, skeleton, in_ledger = parts_judged_for_purpose()
    if in_ledger == 0:
        print("     NOT MEASURED  no ledger at docs/part-purpose-audit.md")
    else:
        looked_at = serving + skeleton
        print(f"     {'parts judged':<26} {looked_at:>4} of {in_ledger}")
        print(f"     {'serving their purpose':<26} {serving:>4}")
        print(f"     {'found to be skeletons':<26} {skeleton:>4}")
        print(f"     {'nobody has looked yet':<26} {in_ledger - looked_at:>4}")
        print(
            "\n     A different question from every number above it. Those measure\n"
            "     whether data flows; this asks whether what flows is worth anything.\n"
            "     A part can be RUNNING, every wire can read CARRYING, all four\n"
            "     checkers can pass, and it can still publish a number that means\n"
            "     nothing -- a climbing counter proves a message moved, never that\n"
            "     the message was right. Real data only (RL-063): a fixture is\n"
            "     exactly what makes a hollow part look healthy."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
