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
LEGITIMATELY_CRYPTO = (
    "runtime/venues/binance_usdm.py",
    "runtime/venues/bybit_linear.py",
    "runtime/venues/adapter_registry.py",
    "dashboard/measure_objectives.py",
)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
