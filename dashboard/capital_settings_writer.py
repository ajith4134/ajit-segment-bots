#!/usr/bin/env python3
"""Changing a capital setting from the board, safely enough to be worth doing.

RL-051 asked for these editable from the dashboard. RL-055 had earlier answered
that they are edited in a file over SSH and the board only displays them, and the
capital-desk proposal hardened that into *the dashboard is a data producer, never
a control switch*. The operator reversed that on 2026-08-25.

**The premise changed underneath the reversal, and that is why this is built the
way it is.** RL-055 assumed an SSH tunnel. The board is now on a public URL, so an
edit form there is a public write endpoint that can move capital limits and flip
paper to live. Every guard below exists because of that, and a path missing any
one of them is not the path that was agreed:

1. **Password.** Checked against the scrypt hash in `dashboard/board_password.py`,
   which lives outside every repository.
2. **Rate limit that widens.** A thirteen-character password is strong against a
   person and weak against a machine allowed to guess without limit, so the
   refusal gets slower each time and the door is what enforces it.
3. **Validated before it lands**, by the same rules `capital-settings-validator`
   applies -- a minimum above a maximum, a maximum above the allocation, an
   allocation above the balance, a ceiling below unlevered. A settings file that
   contradicts itself zeroes the risk limit, so writing one is not a small error.
4. **Journalled** -- not by this module, which would be a second writer of the
   same truth. The board writes the file; `capital-settings-change-recorder` sees
   the value move on its next read and journals it, which keeps that journal the
   single source RL-055 requires.
5. **The live switch is louder than everything else** and is refused here. RL-005
   makes paper-first the method; `money_mode` is changed on the server, by a human
   who went there on purpose.

**R-01 is untouched.** No *part* writes settings -- `main-account.toml` says so
and the blueprint enforces it. The board is off-diagram substrate, like the tape
and the settings themselves, so a board that writes a file no part may write
breaks nothing. That distinction is the whole reason this is here and not in a
part.

**The note is preserved and extended, never replaced.** `load_settings_document`
refuses any entry missing `value`, `unit` or `note`, so a writer that dropped the
note would make the file unreadable to every part at once. Each edit appends its
own line to the note: who changed it, when, and from what.
"""

from __future__ import annotations

import pathlib
import re
import sys
import time
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (str(PROJECT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Refused outright, whatever the password says. RL-005 makes paper-first the
# method rather than a preference, and a public endpoint is not where a segment
# starts trading real money.
REFUSED_FROM_THE_BOARD = ("money_mode",)

# What each setting is, so a value can be checked before it is written. A setting
# absent here cannot be edited at all: an allowlist refuses the thing nobody
# thought of, where a blocklist admits it.
EDITABLE = {
    ("main-account", "main_balance"): float,
    ("main-account", "maximum_capital_per_trade"): float,
    ("main-account", "leverage_ceiling"): float,
    ("futures", "allocated_balance"): float,
    ("futures", "minimum_capital_per_trade"): float,
    ("futures", "maximum_capital_per_trade"): float,
    ("futures", "leverage_ceiling"): float,
}

SCOPE_FILES = {
    "main-account": "main-account.toml",
    "futures": "segments/futures.toml",
}

ACCEPTED = "accepted"
REFUSED_NOT_EDITABLE = "this setting is not editable from the board"
REFUSED_REAL_MONEY = "this setting decides whether real money moves"
REFUSED_NOT_A_NUMBER = "the value is not a number"
REFUSED_CONTRADICTS = "the settings would contradict each other"
REFUSED_UNCHANGED = "the value is already that"


@dataclass(frozen=True)
class WriteVerdict:
    """Whether a change may be written, and the reason when it may not."""

    is_accepted: bool
    reason: str
    faults: tuple = ()

    def as_dict(self) -> dict:
        return {"accepted": self.is_accepted, "reason": self.reason, "faults": list(self.faults)}


@dataclass
class AttemptLimiter:
    """Refuses faster than a machine can guess, and widens each time it refuses.

    Held per process rather than per connection: an attacker opening a new
    connection for each guess would otherwise reset the delay every time, which is
    the failure a naive limiter has.

    The wait doubles and is capped, because an unbounded wait locks the operator
    out permanently after a few typos -- which is a denial of service the attacker
    did not have to achieve themselves.
    """

    first_wait_seconds: float = 1.0
    longest_wait_seconds: float = 300.0
    _failures: int = 0
    _blocked_until: float = 0.0
    _now: object = time.monotonic

    def seconds_remaining(self) -> float:
        return max(0.0, self._blocked_until - self._now())

    def is_blocked(self) -> bool:
        return self.seconds_remaining() > 0

    def record_failure(self) -> float:
        self._failures += 1
        wait = min(self.first_wait_seconds * (2 ** (self._failures - 1)), self.longest_wait_seconds)
        self._blocked_until = self._now() + wait
        return wait

    def record_success(self) -> None:
        self._failures = 0
        self._blocked_until = 0.0

    @property
    def failures(self) -> int:
        return self._failures


def read_number(value) -> float | None:
    """A number, or None. A string that is not one is not silently a zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def judge_change(scope: str, name: str, new_value, current: dict) -> WriteVerdict:
    """Whether this change may be written, by the validator's own rules.

    `current` is every editable value as it stands, keyed `(scope, name)`, so the
    cross-checks can be applied to the file as it *would be* rather than as it is.
    A change that is individually sensible and jointly contradictory is exactly
    what these checks exist to catch.
    """
    if name in REFUSED_FROM_THE_BOARD:
        return WriteVerdict(False, REFUSED_REAL_MONEY)
    if (scope, name) not in EDITABLE:
        return WriteVerdict(False, REFUSED_NOT_EDITABLE)

    number = read_number(new_value)
    if number is None:
        return WriteVerdict(False, REFUSED_NOT_A_NUMBER)
    if number < 0:
        return WriteVerdict(False, "a capital setting is not negative")

    proposed = dict(current)
    proposed[(scope, name)] = number

    faults = find_contradictions(proposed)
    if faults:
        return WriteVerdict(False, REFUSED_CONTRADICTS, tuple(faults))

    if read_number(current.get((scope, name))) == number:
        return WriteVerdict(False, REFUSED_UNCHANGED)

    return WriteVerdict(True, ACCEPTED)


def find_contradictions(values: dict) -> list[str]:
    """The same contradictions capital-settings-validator refuses to trade under.

    Stated here rather than imported from the part, because this is the board and
    a board that imported a part would be wiring itself into the diagram (T-4).
    The rules are the part's and are named as such so the two can be compared.
    """
    faults: list[str] = []
    get = lambda scope, name: read_number(values.get((scope, name)))  # noqa: E731

    minimum = get("futures", "minimum_capital_per_trade")
    maximum = get("futures", "maximum_capital_per_trade")
    allocated = get("futures", "allocated_balance")
    balance = get("main-account", "main_balance")

    if minimum is not None and maximum is not None and minimum > maximum:
        faults.append(
            f"a minimum of {minimum:,.2f} is above the maximum of {maximum:,.2f}; "
            f"no order size satisfies both"
        )
    if maximum is not None and allocated is not None and allocated > 0 and maximum > allocated:
        faults.append(
            f"one trade may use {maximum:,.2f} of an allocation of {allocated:,.2f}"
        )
    if allocated is not None and balance is not None and allocated > balance:
        faults.append(
            f"this segment is allocated {allocated:,.2f} of a main balance of "
            f"{balance:,.2f}; the money is not there"
        )
    for scope in ("main-account", "futures"):
        ceiling = get(scope, "leverage_ceiling")
        if ceiling is not None and ceiling < 1.0:
            faults.append(f"a {scope} ceiling of {ceiling} is below unlevered; 1.0 is the floor")
    return faults


def write_setting(scope: str, name: str, new_value: float, changed_by: str = "the board") -> pathlib.Path:
    """Replace one value in place, keeping the file readable and its note intact.

    Rewrites the one `value =` line inside the named `[section]` and appends a
    sentence to that entry's note. Everything else in the file -- comments,
    ordering, other entries -- is byte-identical, because a writer that
    re-serialised the document would silently drop the commentary that is most of
    what these files are for.

    Written to a temporary file and renamed, so a part reading concurrently sees
    either the old file or the new one and never half of either.
    """
    from runtime.settings_reader import settings_directory

    path = settings_directory() / SCOPE_FILES[scope]
    text = path.read_text(encoding="utf-8")

    section = re.search(rf"^\[{re.escape(name)}\]$", text, flags=re.M)
    if section is None:
        raise KeyError(f"{path} has no [{name}] section; refusing to invent one")

    start = section.end()
    following = re.search(r"^\[", text[start:], flags=re.M)
    end = start + (following.start() if following else len(text) - start)
    body = text[start:end]

    value_line = re.search(r"^value\s*=\s*.*$", body, flags=re.M)
    if value_line is None:
        raise KeyError(f"[{name}] in {path} has no value line; refusing to add one")
    previous = value_line.group(0).split("=", 1)[1].strip()

    stamped = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    body = body[: value_line.start()] + f"value = {new_value}" + body[value_line.end() :]

    # `[ \t]*$` and never `\s*$`. In MULTILINE, `\s` matches a newline and `$`
    # matches at the next line boundary, so a greedy `\s*$` swallows the line
    # break and the following section header lands on the end of the note --
    # which is invalid TOML and makes the whole file unreadable to every part.
    # Caught on 2026-08-25 by writing to a copy of the operator's real file
    # rather than only to a synthetic one, which had no line after the note.
    note_line = re.search(r'^note\s*=\s*"(.*)"[ \t]*$', body, flags=re.M)
    if note_line is None:
        raise KeyError(
            f"[{name}] in {path} has no note; settings_reader refuses an entry without one, "
            f"so writing this would make the file unreadable to every part at once"
        )
    # Appended, never replaced. The note is the provenance and most of the file.
    extended = (
        f"{note_line.group(1)} Changed from {previous} to {new_value} by {changed_by} "
        f"at {stamped} UTC."
    )
    body = body[: note_line.start()] + f'note  = "{extended}"' + body[note_line.end() :]

    updated = text[:start] + body + text[end:]
    temporary = path.with_suffix(".partial")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return path


def read_current_values() -> dict:
    """Every editable setting as it stands, for the cross-checks to judge against."""
    from runtime.settings_reader import load_settings_document, settings_directory

    root = settings_directory()
    values: dict = {}
    for scope, relative in SCOPE_FILES.items():
        try:
            document = load_settings_document(root / relative, scope)
        except Exception:
            continue
        for (entry_scope, entry_name) in EDITABLE:
            if entry_scope != scope:
                continue
            entry = document.entries.get(entry_name)
            if entry is not None:
                values[(scope, entry_name)] = entry.value
    return values
