"""broker-account-funds-reader: a broker's live account funds, self-contained.

GET /v2/user/get-funds-and-margin needs only a valid token -- no order, no
instrument (spec section 6). Direct analogue of venue-balance-reader's
staleness discipline: a reading past its freshness bound is reported as
stale rather than served as current, because a stale figure looks exactly
like a fresh one and would size against money that may already be spent.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-account-funds-reader"

PART_DECLARATION = PartDeclaration(
    part_id="broker-account-funds-reader",
    consumes=("broker-token-standing",),
    produces=("broker-account-funds", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FRESH = "fresh"
STALE = "stale"
UNREADABLE = "unreadable"
NO_TOKEN = "no-token"

FUNDS_URL = "https://api.upstox.com/v2/user/get-funds-and-margin"


@dataclass(frozen=True)
class AccountFunds:
    """What a broker says is available in one segment, and how old that is."""

    broker_id: str
    segment: str
    state: str
    used_margin: float | None
    payin_amount: float | None
    span_margin: float | None
    adhoc_margin: float | None
    notional_cash: float | None
    available_margin: float | None
    exposure_margin: float | None
    age_seconds: float | None
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == FRESH


@dataclass
class _Reading:
    by_segment: dict  # segment -> dict of the seven margin fields
    at_monotonic: float


@dataclass
class FundsStanding:
    reads: int = 0
    failures: int = 0
    stale_reads: int = 0
    refused_no_token: int = 0
    last_failure: str | None = None


def fetch_funds(access_token: str, timeout_seconds: float = 30.0) -> dict:
    request = urllib.request.Request(
        FUNDS_URL,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        import json

        return json.loads(response.read().decode("utf-8"))


class BrokerAccountFundsReader:
    """Fetches one broker's account funds, refusing to serve a reading past its age."""

    def __init__(
        self,
        broker_id: str,
        fetch,
        freshness_seconds: float,
        has_valid_token=lambda: True,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._broker_id = broker_id
        self._fetch = fetch
        self._freshness = freshness_seconds
        self._has_valid_token = has_valid_token
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._last: _Reading | None = None
        self.standing = FundsStanding()

    def read(self) -> tuple[AccountFunds, ...]:
        """Fetch this broker's funds now, or say why there is no usable figure."""
        if not self._has_valid_token():
            self.standing.refused_no_token += 1
            return self._readings(NO_TOKEN, None, "no valid token for this broker")

        self.standing.reads += 1
        try:
            response = self._fetch()
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"{self._broker_id}: {type(failure).__name__}: {failure}"
            return self._serve_last_known(f"{type(failure).__name__}: {failure}")

        by_segment = {
            segment: {
                field: values.get(field)
                for field in (
                    "used_margin", "payin_amount", "span_margin", "adhoc_margin",
                    "notional_cash", "available_margin", "exposure_margin",
                )
            }
            for segment, values in (response.get("data") or {}).items()
        }
        self._last = _Reading(by_segment=by_segment, at_monotonic=self._monotonic())
        return self._readings(FRESH, self._last, "read from the broker just now")

    def _serve_last_known(self, failure_reason: str) -> tuple[AccountFunds, ...]:
        if self._last is None:
            return self._readings(UNREADABLE, None, failure_reason)
        age = self._monotonic() - self._last.at_monotonic
        if age > self._freshness:
            self.standing.stale_reads += 1
            return self._readings(
                STALE, self._last,
                f"{failure_reason}; the last reading is {age:.1f}s old, past {self._freshness:.0f}s",
            )
        return self._readings(FRESH, self._last, f"{failure_reason}; serving a reading {age:.1f}s old")

    def _readings(
        self, state: str, reading: _Reading | None, reason: str
    ) -> tuple[AccountFunds, ...]:
        age = None if reading is None else self._monotonic() - reading.at_monotonic
        if reading is None:
            return (AccountFunds(
                broker_id=self._broker_id, segment="equity", state=state,
                used_margin=None, payin_amount=None, span_margin=None,
                adhoc_margin=None, notional_cash=None, available_margin=None,
                exposure_margin=None, age_seconds=age, reason=reason,
                read_at_ns=self._now_ns(),
            ),)
        return tuple(
            AccountFunds(
                broker_id=self._broker_id, segment=segment, state=state,
                used_margin=values["used_margin"], payin_amount=values["payin_amount"],
                span_margin=values["span_margin"], adhoc_margin=values["adhoc_margin"],
                notional_cash=values["notional_cash"],
                available_margin=values["available_margin"],
                exposure_margin=values["exposure_margin"],
                age_seconds=age, reason=reason, read_at_ns=self._now_ns(),
            )
            for segment, values in sorted(reading.by_segment.items())
        )


def describe_standing(reader: BrokerAccountFundsReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads": reader.standing.reads,
        "failures": reader.standing.failures,
        "stale_reads": reader.standing.stale_reads,
        "refused_no_token": reader.standing.refused_no_token,
        "last_failure": reader.standing.last_failure,
    }


def start_part(context) -> int:
    """T-1's one entry point. Freshness bound is a named setting (RL-061);
    the reader refuses to serve a reading older than it, same discipline as
    venue-balance-reader.
    """
    from runtime.brokers.upstox import UPSTOX_BROKER_ID
    from runtime.input_assembly import LatestByKey

    token_standing = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
    )
    publish_funds = context.bus.publisher_for("broker-account-funds")
    freshness_seconds = context.number("broker_account_funds_freshness")

    def current_token():
        return token_standing.mapping().get(UPSTOX_BROKER_ID)

    reader = BrokerAccountFundsReader(
        broker_id=UPSTOX_BROKER_ID,
        fetch=lambda: fetch_funds(current_token().access_token),
        freshness_seconds=freshness_seconds,
        has_valid_token=lambda: (t := current_token()) is not None and t.is_still_valid(),
    )
    last_read = [float("-inf")]

    def tick() -> None:
        import time as clock

        now = clock.monotonic()
        if now - last_read[0] < freshness_seconds:
            return
        last_read[0] = now
        publish_funds(reader.read())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(reader),
    )


__all__ = [
    "AccountFunds",
    "BrokerAccountFundsReader",
    "FRESH",
    "FundsStanding",
    "NO_TOKEN",
    "PART_DECLARATION",
    "PART_ID",
    "STALE",
    "UNREADABLE",
    "describe_standing",
    "fetch_funds",
    "start_part",
]
