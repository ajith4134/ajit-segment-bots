"""venue-balance-reader: the live balance from the venue.

The venue's balance is the only real one. Everything the system believes about
what it can spend is derived from fills it saw, and fills can be missed --
so this part exists to be the thing that is not derived.

It refuses to answer with a stale figure. A balance that is thirty seconds old
looks exactly like a current one and will size an order that cannot be filled;
`fund-lock-ledger` reserving against it would reserve against money that is
already committed. So a reading past its freshness bound is reported as stale
rather than returned, and the caller decides what to do without a number that
lies about its own age.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "venue-balance-reader"

PART_DECLARATION = PartDeclaration(
    part_id="venue-balance-reader",
    consumes=("key-standing",),
    produces=("account-balance", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FRESH = "fresh"
STALE = "stale"
UNREADABLE = "unreadable"
NO_KEY = "no-key"


@dataclass(frozen=True)
class AccountBalance:
    """What one venue says is available, and how old that statement is."""

    venue_id: str
    state: str
    free: float | None
    used: float | None
    total: float | None
    currency: str
    age_seconds: float | None
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == FRESH


@dataclass
class _Reading:
    free: float
    used: float
    total: float
    at_monotonic: float


@dataclass
class BalanceStanding:
    reads: int = 0
    failures: int = 0
    stale_reads: int = 0
    refused_no_key: int = 0
    last_failure: str | None = None
    by_venue: dict = field(default_factory=dict)


class VenueBalanceReader:
    """Fetches the venue's own balance, and refuses to serve one that has aged out."""

    def __init__(
        self,
        clients: dict[str, object],
        read_key_standing,
        settlement_currency: str,
        freshness_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._clients = clients
        self._read_key_standing = read_key_standing
        self._currency = settlement_currency
        self._freshness = freshness_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._last: dict[str, _Reading] = {}
        self.standing = BalanceStanding()

    def read(self, venue_id: str) -> AccountBalance:
        """Fetch this venue's balance now, or say why there is no usable figure."""
        if self._read_key_standing(venue_id) is None:
            self.standing.refused_no_key += 1
            return self._balance(venue_id, NO_KEY, None, "no key is available for this venue")

        client = self._clients.get(venue_id)
        if client is None:
            return self._balance(venue_id, UNREADABLE, None, f"no client is configured for {venue_id}")

        self.standing.reads += 1
        try:
            response = client.fetch_balance()
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"{venue_id}: {type(failure).__name__}: {failure}"
            return self._serve_last_known(venue_id, f"{type(failure).__name__}: {failure}")

        holding = response.get(self._currency) or {}
        reading = _Reading(
            free=float(holding.get("free") or 0.0),
            used=float(holding.get("used") or 0.0),
            total=float(holding.get("total") or 0.0),
            at_monotonic=self._monotonic(),
        )
        self._last[venue_id] = reading
        self.standing.by_venue[venue_id] = self.standing.by_venue.get(venue_id, 0) + 1
        return self._balance(venue_id, FRESH, reading, "read from the venue just now")

    def _serve_last_known(self, venue_id: str, failure_reason: str) -> AccountBalance:
        """A failed fetch falls back to the last reading, marked by its own age."""
        reading = self._last.get(venue_id)
        if reading is None:
            return self._balance(venue_id, UNREADABLE, None, failure_reason)
        age = self._monotonic() - reading.at_monotonic
        if age > self._freshness:
            self.standing.stale_reads += 1
            return self._balance(
                venue_id, STALE, reading,
                f"{failure_reason}; the last reading is {age:.1f}s old, past {self._freshness:.0f}s",
            )
        return self._balance(venue_id, FRESH, reading, f"{failure_reason}; serving a reading {age:.1f}s old")

    def _balance(self, venue_id: str, state: str, reading: _Reading | None, reason: str) -> AccountBalance:
        age = None if reading is None else self._monotonic() - reading.at_monotonic
        return AccountBalance(
            venue_id=venue_id,
            state=state,
            free=reading.free if reading else None,
            used=reading.used if reading else None,
            total=reading.total if reading else None,
            currency=self._currency,
            age_seconds=age,
            reason=reason,
            read_at_ns=self._now_ns(),
        )

    def read_all(self) -> tuple[AccountBalance, ...]:
        return tuple(self.read(venue_id) for venue_id in sorted(self._clients))


def describe_balances(reader: VenueBalanceReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads": reader.standing.reads,
        "failures": reader.standing.failures,
        "stale_reads": reader.standing.stale_reads,
        "refused_no_key": reader.standing.refused_no_key,
        "last_failure": reader.standing.last_failure,
        "by_venue": dict(reader.standing.by_venue),
    }


def run_venue_balance_reader(
    reader: VenueBalanceReader, control_socket, publish_balances,
    health_interval_seconds: float, emit_health,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_balances(reader.read_all()),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
