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
