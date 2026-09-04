"""The account's currency is a fact in the operator's file, not a constant in code.

`MainAccountSettingsReader` took its currency from a constructor default of
`"USDT"`. Nothing ever passed the argument -- `start_part` constructs it bare --
so the currency of the main account was, in practice, the string `"USDT"`
compiled into the reader, whatever `main-account.toml` said.

That was harmless while the account really held USDT. It stopped being harmless
on 2026-09-04, when the file was converted to the index-options segment's INR:
the balance was 500,000 INR and the reader went on calling it USDT.

`paper-currency-converter` is the part that reads it:

    if main is not None and main.currency != quote and rates:
        requests.append({"amount": main.balance, "from_currency": main.currency, ...})

With the segment quoting INR and the account claiming USDT, that asks for a
USDT-to-INR conversion of a number that is already rupees -- a balance wrong by
whatever the rate is (roughly 85x), sourced from a file that stated the right
unit all along.

Every entry in a settings document carries `value`, `unit` and `note` --
`REQUIRED_ENTRY_KEYS` makes the unit mandatory precisely so a number cannot
travel without saying what it measures. Reading it is the fix.
"""

from __future__ import annotations

import pathlib

from parts.capital_desk.main_account_settings_reader import MainAccountSettingsReader

A_MAIN_ACCOUNT_FILE = """
[main_balance]
value = 500000.0
unit  = "{currency}"
note  = "a test's paper figure."

[maximum_capital_per_trade]
value = 100000.0
unit  = "{currency}"
note  = "a test's per-trade ceiling."

[leverage_ceiling]
value = 1.0
unit  = "multiple"
note  = "buy-only options are unlevered by construction."
"""


def a_main_account_file(tmp_path: pathlib.Path, currency: str) -> pathlib.Path:
    path = tmp_path / "main-account.toml"
    path.write_text(A_MAIN_ACCOUNT_FILE.format(currency=currency))
    return path


def test_the_currency_is_read_from_the_balance_entry_unit(tmp_path):
    """The real defect: an INR file was reported as USDT."""
    reader = MainAccountSettingsReader(
        settings_path=a_main_account_file(tmp_path, "INR")
    )

    setting = reader.read()

    assert setting is not None
    assert setting.currency == "INR"
    assert setting.balance == 500000.0


def test_a_usdt_file_still_reads_as_usdt(tmp_path):
    """The unit is read, not replaced -- the old value is not special-cased away."""
    reader = MainAccountSettingsReader(
        settings_path=a_main_account_file(tmp_path, "USDT")
    )

    setting = reader.read()

    assert setting is not None
    assert setting.currency == "USDT"


def test_the_currency_changes_when_the_operator_changes_the_unit(tmp_path):
    """A re-read picks up a currency edit, the same way it picks up a balance edit."""
    path = a_main_account_file(tmp_path, "USDT")
    reader = MainAccountSettingsReader(settings_path=path)
    assert reader.read().currency == "USDT"

    path.write_text(A_MAIN_ACCOUNT_FILE.format(currency="INR"))

    assert reader.read().currency == "INR"
