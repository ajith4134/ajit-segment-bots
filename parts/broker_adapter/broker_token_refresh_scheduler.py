"""broker-token-refresh-scheduler: keep today's broker session token valid.

Runs once daily, before market open, via TOTP auto-login (spec section 3) --
no human click. Same shape as the crypto build's KiteAccessTokenFileStore
precedent (ajith4134/nse-botonly), rebuilt against Upstox's daily 3:30 AM
IST expiry rather than Zerodha's ~6 AM one.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-token-refresh-scheduler"
IST = ZoneInfo("Asia/Kolkata")

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=(),
    produces=("broker-token-standing", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

DEFAULT_TOKEN_FILE_PATH = Path("~/.local/share/ajit-segment-bots/broker_tokens/upstox.json").expanduser()


@dataclasses.dataclass(frozen=True)
class TokenStanding:
    """Whether today's token is still usable right now, and until when.

    `daily_expiry_time_ist` comes from BrokerTokenPolicy.daily_expiry_time_ist
    -- this part does not know Upstox's 3:30 AM figure itself, the adapter
    does (T-4).
    """

    broker_id: str
    access_token: str
    generated_at: datetime.datetime  # timezone-aware
    daily_expiry_time_ist: datetime.time
    last_failure: str | None = None

    def __repr__(self) -> str:  # the token is a secret; never leak it
        return (
            f"TokenStanding(broker_id={self.broker_id!r}, access_token='***', "
            f"generated_at={self.generated_at!r})"
        )

    def expires_at(self) -> datetime.datetime:
        generated_ist = self.generated_at.astimezone(IST)
        same_day_expiry = datetime.datetime.combine(
            generated_ist.date(), self.daily_expiry_time_ist, IST
        )
        if generated_ist < same_day_expiry:
            return same_day_expiry
        return same_day_expiry + datetime.timedelta(days=1)

    def is_still_valid(self, now: datetime.datetime | None = None) -> bool:
        now_ist = (now or datetime.datetime.now(IST)).astimezone(IST)
        return now_ist < self.expires_at()


class TokenFileStore:
    """Persists TokenStanding to a chmod-600 file, outside sops -- it rotates
    daily and doesn't need sops's protection the way a standing password
    does (spec section 8)."""

    def __init__(self, token_file_path: Path = DEFAULT_TOKEN_FILE_PATH) -> None:
        self._token_file_path = token_file_path

    def save(self, standing: TokenStanding) -> None:
        self._token_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_file_path.write_text(json.dumps({
            "broker_id": standing.broker_id,
            "access_token": standing.access_token,
            "generated_at": standing.generated_at.isoformat(),
            "daily_expiry_time_ist": standing.daily_expiry_time_ist.isoformat(),
        }))
        os.chmod(self._token_file_path, 0o600)

    def load(self) -> TokenStanding | None:
        if not self._token_file_path.exists():
            return None
        fields = json.loads(self._token_file_path.read_text())
        return TokenStanding(
            broker_id=fields["broker_id"],
            access_token=fields["access_token"],
            generated_at=datetime.datetime.fromisoformat(fields["generated_at"]),
            daily_expiry_time_ist=datetime.time.fromisoformat(fields["daily_expiry_time_ist"]),
        )


def refresh_if_needed(
    store: TokenFileStore,
    daily_expiry_time_ist: datetime.time,
    generate_token,
    now: datetime.datetime | None = None,
) -> TokenStanding:
    """The scheduling core: refresh only when the stored token is no longer
    valid, keep the stale token on a failed refresh rather than discarding it.

    `generate_token` is a zero-argument callable returning a fresh access
    token string, or raising. Injected so this function never imports
    upstox_totp itself -- start_part below is the only place that does,
    which is what makes this testable without real credentials.
    """
    now = now or datetime.datetime.now(IST)
    existing = store.load()
    if existing is not None and existing.is_still_valid(now):
        return existing
    try:
        token = generate_token()
    except Exception as failure:
        if existing is not None:
            failed = dataclasses.replace(existing, last_failure=f"{type(failure).__name__}: {failure}")
            store.save(failed)
            return failed
        raise
    standing = TokenStanding(
        broker_id="upstox", access_token=token, generated_at=now,
        daily_expiry_time_ist=daily_expiry_time_ist,
    )
    store.save(standing)
    return standing


def describe_standing(standing: TokenStanding | None) -> dict:
    if standing is None:
        return {"part_id": PART_ID, "has_token": False}
    return {
        "part_id": PART_ID,
        "has_token": True,
        "broker_id": standing.broker_id,
        "is_valid": standing.is_still_valid(),
        "generated_at": standing.generated_at.isoformat(),
        "last_failure": standing.last_failure,
    }


def start_part(context) -> int:
    """T-1's one entry point. Reads login credentials from the sops+age store
    (docs/secrets.md), never from settings -- settings are the operator's
    tunable numbers, secrets are secrets, and RUNTIME_SCOPE_NAME's settings
    document only carries the refresh-check interval.
    """
    from upstox_totp import UpstoxTOTP

    store = TokenFileStore()

    def generate_token() -> str:
        upx = UpstoxTOTP()  # auto-loads UPSTOX_* env vars, sourced from the
                             # sops store at process start.
        response = upx.app_token.get_access_token()
        if not response.success or not response.data:
            raise RuntimeError(f"upstox-totp login did not succeed: {response}")
        return response.data.access_token

    publish_standing = context.bus.publisher_for("broker-token-standing")
    # BrokerTokenPolicy's own figure (runtime.brokers.upstox.UpstoxAdapter
    # .token_policy()) -- read directly here rather than instantiating an
    # adapter just for one constant, since this part has no other broker
    # dependency; revisit once a settings-driven adapter registry exists for
    # more than one broker.
    daily_expiry_time_ist = datetime.time(3, 30)
    current: list[TokenStanding | None] = [None]

    def refresh_if_due() -> None:
        current[0] = refresh_if_needed(
            store=store, daily_expiry_time_ist=daily_expiry_time_ist,
            generate_token=generate_token,
        )
        publish_standing(current[0])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=refresh_if_due,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(current[0]),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "TokenFileStore",
    "TokenStanding",
    "describe_standing",
    "refresh_if_needed",
    "start_part",
]
