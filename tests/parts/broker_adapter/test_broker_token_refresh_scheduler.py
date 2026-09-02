import datetime

from zoneinfo import ZoneInfo

from parts.broker_adapter.broker_token_refresh_scheduler import TokenStanding

IST = ZoneInfo("Asia/Kolkata")


def test_token_generated_before_expiry_time_is_valid_same_day():
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)  # 9 AM
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 1, 15, 0, tzinfo=IST)  # 3 PM same day
    assert standing.is_still_valid(check_at)


def test_token_is_invalid_after_next_days_expiry_time():
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 2, 4, 0, tzinfo=IST)  # 4 AM next day
    assert not standing.is_still_valid(check_at)


def test_token_generated_after_expiry_time_expires_the_following_day():
    # Generated at 5 AM -- after that day's 3:30 AM boundary -- so the next
    # boundary is tomorrow's 3:30, not today's already-passed one.
    generated = datetime.datetime(2026, 9, 1, 5, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="tok", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    check_at = datetime.datetime(2026, 9, 2, 3, 0, tzinfo=IST)  # still before tomorrow's 3:30
    assert standing.is_still_valid(check_at)


def test_token_file_store_round_trips(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import TokenFileStore

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    generated = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    standing = TokenStanding(
        broker_id="upstox", access_token="secret-token", generated_at=generated,
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    store.save(standing)

    loaded = store.load()
    assert loaded.access_token == "secret-token"
    assert loaded.broker_id == "upstox"
    assert loaded.generated_at == generated


def test_token_file_store_is_owner_only(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import TokenFileStore
    import stat

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="secret", generated_at=datetime.datetime.now(IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    mode = stat.S_IMODE((tmp_path / "upstox.json").stat().st_mode)
    assert mode == 0o600


def test_refresh_if_needed_skips_when_token_still_valid(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    now = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    store.save(TokenStanding(
        broker_id="upstox", access_token="still-good",
        generated_at=datetime.datetime(2026, 9, 1, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))

    calls = []
    def fake_generate_token():
        calls.append(1)
        return "should-not-be-called"

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=fake_generate_token, now=now,
    )
    assert standing.access_token == "still-good"
    assert calls == []


def test_refresh_if_needed_refreshes_when_token_expired(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="stale",
        generated_at=datetime.datetime(2026, 8, 31, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    now = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=IST)  # past yesterday's expiry

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=lambda: "fresh-token", now=now,
    )
    assert standing.access_token == "fresh-token"
    assert store.load().access_token == "fresh-token"


def test_refresh_if_needed_records_failure_and_keeps_the_stale_token(tmp_path):
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    store.save(TokenStanding(
        broker_id="upstox", access_token="stale",
        generated_at=datetime.datetime(2026, 8, 31, 9, 0, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    ))
    now = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=IST)

    def failing_generate_token():
        raise RuntimeError("login failed: bad TOTP")

    standing = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=failing_generate_token, now=now,
    )
    # The stale token is kept and returned rather than dropped -- a failed
    # refresh must not leave every consumer with nothing at all.
    assert standing.access_token == "stale"
    assert "bad TOTP" in standing.last_failure


def test_refresh_if_needed_never_raises_on_first_ever_failure(tmp_path):
    # No stored token at all yet -- e.g. credentials not configured (this
    # project's real state right now: placeholders in secrets.enc.yaml,
    # docs/secrets.md). Found via the full integration test suite: the part
    # crashed on its very first tick because this branch used to re-raise,
    # taking the whole process down (launched.is_running went False) instead
    # of staying up to report the failure.
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenFileStore, RefreshStanding, refresh_if_needed,
    )

    store = TokenFileStore(token_file_path=tmp_path / "upstox.json")
    refresh_standing = RefreshStanding()

    def failing_generate_token():
        raise RuntimeError("UPSTOX_USERNAME not set")

    result = refresh_if_needed(
        store=store, daily_expiry_time_ist=datetime.time(3, 30),
        generate_token=failing_generate_token,
        standing=refresh_standing,
        now=datetime.datetime(2026, 9, 1, 8, 0, tzinfo=IST),
    )
    assert result is None  # nothing to publish yet -- not a crash
    assert refresh_standing.refresh_attempts == 1
    assert "UPSTOX_USERNAME" in refresh_standing.last_failure
    assert store.load() is None  # never had anything to save


def test_is_refresh_due_true_with_no_attempt_yet():
    """The very first tick always gets to try."""
    from parts.broker_adapter.broker_token_refresh_scheduler import is_refresh_due

    assert is_refresh_due(
        current=None, last_attempt_at=None,
        now_monotonic=1000.0, check_interval_seconds=300.0,
    )


def test_is_refresh_due_false_before_the_check_interval_elapses():
    """Real regression, 2026-09-02: checking on every tick with no token yet
    hit Upstox's own OTP-generation rate limit (UDAPI100500) within minutes,
    long before this box ever saw a real token. 193 attempts in under 15
    minutes on the live spine."""
    from parts.broker_adapter.broker_token_refresh_scheduler import is_refresh_due

    assert not is_refresh_due(
        current=None, last_attempt_at=1000.0,
        now_monotonic=1010.0, check_interval_seconds=300.0,
    )


def test_is_refresh_due_true_once_the_check_interval_has_elapsed():
    from parts.broker_adapter.broker_token_refresh_scheduler import is_refresh_due

    assert is_refresh_due(
        current=None, last_attempt_at=1000.0,
        now_monotonic=1301.0, check_interval_seconds=300.0,
    )


def test_is_refresh_due_true_and_free_when_the_current_token_is_still_valid():
    """A valid token is never throttled -- refresh_if_needed's own
    is_still_valid check already makes re-checking it free, so this must
    stay true regardless of last_attempt_at (over-checking a valid token
    was never the problem; over-checking with none was)."""
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenStanding, is_refresh_due,
    )

    valid = TokenStanding(
        broker_id="upstox", access_token="tok",
        generated_at=datetime.datetime.now(IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    assert is_refresh_due(
        current=valid, last_attempt_at=1000.0,
        now_monotonic=1000.5, check_interval_seconds=300.0,
    )


def test_relax_upstox_totp_poa_field_lets_a_real_response_parse():
    """Real bug, 2026-09-02: upstox-totp 1.0.8 (the latest release on PyPI --
    verified no newer version exists) requires `poa` on every access-token
    response, but a real Upstox login response does not send it. Confirmed
    against a real account: every other required field validated, `poa`
    alone was reported missing. `poa` is never read by this part -- only
    access_token is -- so relaxed rather than waiting on an upstream fix
    that may never land.

    Goes through AccessTokenResponse, the real path get_access_token()
    uses -- not AccessTokenData alone. Patching only AccessTokenData looked
    like it worked in isolation but still failed through the wrapper:
    pydantic's generic model machinery (ResponseBase[AccessTokenData])
    caches its own compiled schema independently of the field it wraps."""
    from upstox_totp.models import AccessTokenResponse

    from parts.broker_adapter.broker_token_refresh_scheduler import (
        relax_upstox_totp_poa_field,
    )

    relax_upstox_totp_poa_field()

    payload = {
        "success": True,
        "data": {
            "email": "test@example.com", "exchanges": ["NSE"], "products": ["D"],
            "broker": "UPSTOX", "user_id": "AB1234", "user_name": "Test User",
            "order_types": ["MARKET"], "user_type": "individual",
            "ddpi": True, "is_active": True, "access_token": "fake-token-value",
            # poa deliberately absent, matching the real response shape
        },
    }
    parsed = AccessTokenResponse(**payload)
    assert parsed.success is True
    assert parsed.data.access_token == "fake-token-value"
    assert parsed.data.poa is None


def test_relax_upstox_totp_poa_field_is_idempotent():
    """Called once per process at start_part, but must not raise or
    double-patch if called again -- guards against a future change calling
    it more than once."""
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        relax_upstox_totp_poa_field,
    )

    relax_upstox_totp_poa_field()
    relax_upstox_totp_poa_field()
    relax_upstox_totp_poa_field()


def test_is_refresh_due_false_when_the_current_token_has_expired():
    from parts.broker_adapter.broker_token_refresh_scheduler import (
        TokenStanding, is_refresh_due,
    )

    expired = TokenStanding(
        broker_id="upstox", access_token="tok",
        generated_at=datetime.datetime(2020, 1, 1, tzinfo=IST),
        daily_expiry_time_ist=datetime.time(3, 30),
    )
    assert not is_refresh_due(
        current=expired, last_attempt_at=1000.0,
        now_monotonic=1010.0, check_interval_seconds=300.0,
    )
