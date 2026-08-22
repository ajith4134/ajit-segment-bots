"""main-account-settings-reader: the main balance and its currency, reloaded on change.

RL-055: capital settings are edited in a file on the server over SSH, and the
board shows the current values and when they last changed. This is the part that
reads them.

**It ships at zero.** A main balance of zero means nothing is allocated, which is
the only safe state for a system that has not been told to trade with real money.
Every downstream allocation is a fraction of this number, so zero here makes the
whole system structurally unable to size a real trade -- and that is the correct
default, not an inconvenience to work around.

**A change is detected by content, not by mtime.** A file saved without editing
it is not a change, and touching a file must not look like an operator decision;
equally, an editor that preserves timestamps must not hide one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

PART_ID = "main-account-settings-reader"

PART_DECLARATION = PartDeclaration(
    part_id="main-account-settings-reader",
    consumes=(),
    produces=("main-account-setting", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MAIN_ACCOUNT_FILE = "main-account.toml"

# The settings this file must carry, and what each one bounds. Missing is
# refused rather than defaulted: every one of these is capital.
REQUIRED_SETTINGS = ("main_balance", "maximum_capital_per_trade", "leverage_ceiling")


@dataclass(frozen=True)
class MainAccountSetting:
    """The operator's account, as the settings file states it."""

    balance: float
    currency: str
    maximum_capital_per_trade: float
    leverage_ceiling: float
    content_digest: str
    read_at_ns: int
    changed_at_ns: int | None

    @property
    def can_size_a_real_trade(self) -> bool:
        """False while the balance is zero, which is how this ships."""
        return self.balance > 0 and self.maximum_capital_per_trade > 0


@dataclass
class MainAccountStanding:
    reads: int = 0
    changes_seen: int = 0
    failures: int = 0
    settings_path: str | None = None
    last_failure: str | None = None
    last_change_at_ns: int | None = None
    balance: float | None = None


class MainAccountSettingsReader:
    """Reads the main account file and reports each genuine change to it."""

    def __init__(self, settings_path=None, currency: str = "USDT", now_ns=time.time_ns) -> None:
        self._path = settings_path or (settings_directory() / MAIN_ACCOUNT_FILE)
        self._currency = currency
        self._now_ns = now_ns
        self._current: MainAccountSetting | None = None
        self.standing = MainAccountStanding(settings_path=str(self._path))

    @property
    def current(self) -> MainAccountSetting | None:
        return self._current

    def read(self) -> MainAccountSetting | None:
        """Read the file. Returns the setting, or None if none has ever read cleanly.

        A failed read keeps the last good setting, for the same reason every
        other settings reader here does: an operator mid-edit must not be able to
        change what the system believes it may spend by saving a broken file.
        """
        self.standing.reads += 1
        try:
            document = load_settings_document(self._path, "main-account")
        except SettingsParseRefused as refusal:
            self.standing.failures += 1
            self.standing.last_failure = f"{type(refusal).__name__}: {refusal}"
            return self._current

        missing = [name for name in REQUIRED_SETTINGS if name not in document.entries]
        if missing:
            self.standing.failures += 1
            self.standing.last_failure = (
                f"{self._path} is missing {', '.join(missing)}; every one of these is capital "
                f"and none has a safe default"
            )
            return self._current

        changed = self._current is None or self._current.content_digest != document.content_digest
        if changed and self._current is not None:
            self.standing.changes_seen += 1
            self.standing.last_change_at_ns = self._now_ns()

        self._current = MainAccountSetting(
            balance=float(document.read_value("main_balance")),
            currency=self._currency,
            maximum_capital_per_trade=float(document.read_value("maximum_capital_per_trade")),
            leverage_ceiling=float(document.read_value("leverage_ceiling")),
            content_digest=document.content_digest,
            read_at_ns=self._now_ns(),
            changed_at_ns=self.standing.last_change_at_ns,
        )
        self.standing.balance = self._current.balance
        self.standing.last_failure = None
        return self._current

    def has_changed_since(self, digest: str) -> bool:
        """Whether the file's content differs from a digest a caller holds."""
        return self._current is not None and self._current.content_digest != digest


def describe_main_account(reader: MainAccountSettingsReader) -> dict:
    current = reader.current
    return {
        "part_id": PART_ID,
        "settings_path": reader.standing.settings_path,
        "reads": reader.standing.reads,
        "changes_seen": reader.standing.changes_seen,
        "failures": reader.standing.failures,
        "last_failure": reader.standing.last_failure,
        "last_change_at_ns": reader.standing.last_change_at_ns,
        "balance": current.balance if current else None,
        "currency": current.currency if current else None,
        "can_size_a_real_trade": current.can_size_a_real_trade if current else False,
    }


def run_main_account_settings_reader(
    reader: MainAccountSettingsReader, control_socket, publish_setting,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        setting = reader.read()
        if setting is not None:
            publish_setting(setting)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
