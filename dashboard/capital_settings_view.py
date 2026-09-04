#!/usr/bin/env python3
"""The capital settings, their current values, and when each last changed.

RL-040, RL-041, RL-051 and RL-053 are the operator's: the paper balance, what one
trade may commit at least and at most, and the leverage ceiling the bot chooses
under. RL-055 decided where they are edited and what the board owes them --

    the board shows the current values and when they last changed

-- and `docs/settings-schema.md` names the one legal source for the second half:

    which is the only part the board's "when it last changed" answer is allowed
    to come from -- never the file's mtime, never `git log`

so that is where it is read from. A file's mtime says when it was *saved*, which
is a different question and often a misleading one: a file rewritten with the same
contents has a new mtime and no change in it, and a value edited an hour before the
part restarted has an mtime that says nothing about when the system began acting
on it.

**Reading is separate from writing on purpose.** This module only reads. The write
path is password-gated, rate-limited, validated and journalled, and keeping the two
apart means a board that can only display cannot be turned into one that edits by
accident.

**Never invents a "last changed".** A setting the recorder has not journalled
reports `NOT MEASURED` rather than the file's timestamp or the run's start. The
difference between "this has never moved" and "nobody watched it move" is the
whole reason the journal is the source.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (str(PROJECT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from capital_settings_writer import editable_settings, REFUSED_FROM_THE_BOARD  # noqa: E402

NOT_MEASURED = "NOT MEASURED"

# What the operator is entitled to see and change, per scope. Named rather than
# "whatever is in the file": a settings file also holds thresholds, window lengths
# and paths, and a board that offered all of them as capital controls would invite
# an edit to something that is not one.
# The file's own keys, checked against it rather than guessed: the recorder
# journals main-account.balance while the file calls it main_balance, and a
# board naming the journal's spelling silently showed two fewer settings than
# the operator has.
MAIN_ACCOUNT_SETTINGS = ("main_balance", "maximum_capital_per_trade", "leverage_ceiling")
SEGMENT_SETTINGS = (
    "money_mode",
    "allocated_balance",
    "minimum_capital_per_trade",
    "maximum_capital_per_trade",
    "leverage_ceiling",
    "quote_currency",
)

# Settings that decide whether real money can move. Marked so the board can treat
# them differently from a number that only bounds a paper trade (RL-005).
MOVES_REAL_MONEY = ("money_mode",)


@dataclass(frozen=True)
class SettingView:
    """One setting as the operator sees it: its value, its note, and its history."""

    scope: str
    name: str
    value: object
    unit: str
    note: str
    last_changed_at_ns: int | None
    previous_value: object
    change_count: int
    moves_real_money: bool
    is_editable: bool
    not_editable_reason: str

    def as_dict(self) -> dict:
        age = None
        if self.last_changed_at_ns:
            age = (time.time_ns() - self.last_changed_at_ns) / 1e9
        return {
            "scope": self.scope,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "note": self.note,
            "last_changed_at_ns": self.last_changed_at_ns,
            "last_changed_age_seconds": age,
            # Said as its own state. A setting the recorder has never journalled
            # is not a setting that has never moved.
            "last_changed_is_measured": self.last_changed_at_ns is not None,
            "previous_value": self.previous_value,
            "change_count": self.change_count,
            "moves_real_money": self.moves_real_money,
            "is_editable": self.is_editable,
            "not_editable_reason": self.not_editable_reason,
        }


def read_settings_journal() -> tuple[dict, dict]:
    """Every capital setting's change history, from the recorder that journalled it.

    Keyed `(scope, setting)`, newest last. This is the only source the board's
    "when it last changed" may use, and the reason is in the module docstring.
    """
    from build_trade_board import STATE_DIRECTORY

    path = STATE_DIRECTORY / "journal.capital-settings-change-recorder.sqlite"
    if not path.exists():
        return {}, {
            "ok": False,
            "proof": (
                f"no journal at {path}: capital-settings-change-recorder has not "
                f"recorded anything, so when a setting last changed is unknown "
                f"rather than never"
            ),
        }

    history: dict[tuple[str, str], list[dict]] = {}
    entries = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") or {}
        scope, setting = payload.get("scope"), payload.get("setting")
        if not scope or not setting:
            continue
        history.setdefault((scope, setting), []).append(payload)
        entries += 1

    return history, {
        "ok": True,
        "proof": (
            f"{entries} entry(s) from {path.name} -- the journal "
            f"capital-settings-change-recorder writes, which is the only source "
            f"this answer may come from (RL-055)"
        ),
    }


def segment_scope_name() -> str:
    """Which segment's capital this board is about, as `segment_id` names it.

    Read rather than hardcoded, because this board hardcoded `"futures"` and
    went on showing the retired crypto segment after `segment_id` became
    `index-options` on 2026-09-02 -- serving 1,000 USDT and 50 USDT from a file
    whose own note says it is "not read by anything now", and never showing the
    500,000 INR the bot actually sizes against.

    This is the same setting every per-segment part reads to find its own file,
    so the board and the bot cannot disagree about which segment is trading.
    """
    # Imported inside the function, not at module scope: this module is loaded
    # by part_health_api with dashboard/ on the path rather than the repo root,
    # so a top-level `import runtime...` crash-loops the board service. Every
    # other runtime import in this file is function-local for the same reason.
    from runtime.part_context import RUNTIME_SCOPE
    from runtime.settings_reader import load_settings_document, settings_directory

    document = load_settings_document(
        settings_directory() / "runtime.toml", RUNTIME_SCOPE
    )
    return str(document.read_value("segment_id"))


def segment_settings_path() -> str:
    """The segment file's path relative to the settings directory."""
    return f"segments/{segment_scope_name()}.toml"


def read_settings_documents() -> tuple[dict, dict]:
    """The live settings, as the parts read them, with each entry's own note."""
    from runtime.settings_reader import load_settings_document, settings_directory

    root = settings_directory()
    documents: dict[str, object] = {}
    problems: dict[str, str] = {}

    for scope, relative in (
        ("main-account", "main-account.toml"),
        (segment_scope_name(), segment_settings_path()),
    ):
        try:
            documents[scope] = load_settings_document(root / relative, scope)
        except Exception as refusal:
            problems[scope] = str(refusal)
    return documents, problems


def build_capital_settings_view() -> dict:
    """What the operator may see, per scope, with the provenance of every number."""
    history, journal_provenance = read_settings_journal()
    documents, problems = read_settings_documents()

    scopes = []
    for scope, wanted in (
        ("main-account", MAIN_ACCOUNT_SETTINGS),
        (segment_scope_name(), SEGMENT_SETTINGS),
    ):
        document = documents.get(scope)
        if document is None:
            scopes.append({
                "scope": scope,
                "ok": False,
                "proof": problems.get(scope, "the settings file could not be read"),
                "settings": [],
            })
            continue

        views = []
        for name in wanted:
            entry = document.entries.get(name)
            if entry is None:
                continue
            changes = history.get((scope, name), [])
            # Only real changes carry a "last changed". A first reading is the
            # recorder noticing a value, not the operator moving one.
            moved = [c for c in changes if c.get("previous_value") is not None]
            latest = moved[-1] if moved else None
            views.append(SettingView(
                scope=scope,
                name=name,
                value=entry.value,
                unit=getattr(entry, "unit", "") or "",
                note=getattr(entry, "note", "") or "",
                last_changed_at_ns=latest.get("changed_at_ns") if latest else None,
                previous_value=latest.get("previous_value") if latest else None,
                change_count=len(moved),
                moves_real_money=name in MOVES_REAL_MONEY,
                # Asked of the writer rather than restated here. Two lists of
                # what may be edited would drift, and the one that drifted
                # would be this one -- the board is not where that answer lives.
                is_editable=(scope, name) in editable_settings() and name not in REFUSED_FROM_THE_BOARD,
                not_editable_reason=reason_not_editable(scope, name),
            ).as_dict())

        scopes.append({"scope": scope, "ok": True, "proof": f"{root_of(document)}", "settings": views})

    return {
        "scopes": scopes,
        "journal": journal_provenance,
        "editable": True,
        "editable_reason": (
            "a change is password-gated, rate-limited, checked against the same "
            "contradictions capital-settings-validator refuses to trade under, and "
            "journalled by capital-settings-change-recorder on its next read"
        ),
    }


def reason_not_editable(scope: str, name: str) -> str:
    """Why a setting has no input beside it, said rather than left blank."""
    if name in REFUSED_FROM_THE_BOARD:
        return (
            "changed on the server, not from here. RL-005 makes paper-first the "
            "method, and a public URL is not where a segment starts trading real money"
        )
    if (scope, name) not in editable_settings():
        return "not one of the capital settings this board may change"
    return ""


def root_of(document) -> str:
    return str(getattr(document, "path", "the operator's settings directory"))


if __name__ == "__main__":
    view = build_capital_settings_view()
    print(view["journal"]["proof"], "\n")
    for scope in view["scopes"]:
        print(f"[{scope['scope']}]  {'' if scope['ok'] else scope['proof']}")
        for setting in scope["settings"]:
            when = (
                f"changed {setting['last_changed_age_seconds'] / 3600:.1f}h ago "
                f"from {setting['previous_value']}"
                if setting["last_changed_is_measured"] else f"{NOT_MEASURED}: never journalled a change"
            )
            flag = "  [MOVES REAL MONEY]" if setting["moves_real_money"] else ""
            print(f"   {setting['name']:<28} {str(setting['value']):<12} {setting['unit']:<10} {when}{flag}")
        print()
