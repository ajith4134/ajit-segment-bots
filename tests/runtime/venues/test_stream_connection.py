"""The connection, against a real websocket server that closes on purpose.

The behaviour under test is what happens when a connection ends -- scheduled,
unexpected, or refused outright -- and no captured tape contains that. So these
run against a real `websockets` server on localhost that closes when told to:
real frames, real close codes, real reconnects. RL-063 is about not inventing
market data, and none is invented here; the messages are the server's own and
nothing reads them as prices.

The clock is injected because a Binance connection's scheduled close comes at 24
hours, and a test that waited for it would not be a test.
"""

import json
import threading
import time

import pytest
from websockets.sync.server import serve

from runtime.tape import StreamKind
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.stream_connection import (
    ConnectionRateWindow,
    ConnectionRefused,
    VenueStreamConnection,
)
from runtime.venues.venue_adapter import StreamRequest

BACKOFF_FLOOR = 1.0
BACKOFF_CEILING = 60.0
DRAIN_INTERVAL = 0.5

A_SYMBOL = "BTCUSDT"
A_CANDLE_INTERVAL = "1m"
A_BOOK_DEPTH = 20


class FakeClock:
    """A monotonic clock the test moves, and a sleep that moves it.

    Sleeping for real would make every backoff assertion a delay. Moving the
    clock instead means the waits are asserted rather than endured.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ServerUnderTest:
    """A real websocket server that says what it received and closes on command."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.connections_accepted = 0
        self._live = []
        self._server = None
        self._thread = None

    def close_live_connections(self, code: int = 1000) -> None:
        """Close from the server end, the way a venue's scheduled disconnect does."""
        for connection in list(self._live):
            connection.close(code=code)

    def __enter__(self) -> "ServerUnderTest":
        from websockets.sync.client import connect

        self._server = serve(self._handle, "127.0.0.1", 0)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        # Prove it is accepting before handing it to a test. Without this, a fast
        # test can shut the server down while its thread is still inside
        # serve_forever's own startup, and the socket it is about to read is
        # already closed -- which surfaces as an unrelated thread exception in
        # whichever test happened to be quick that run.
        with connect(self.url, open_timeout=5, close_timeout=1):
            pass
        self.connections_accepted = 0
        return self

    def __exit__(self, *exception) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.socket.getsockname()[:2]
        return f"ws://{host}:{port}"

    def _handle(self, connection) -> None:
        self.connections_accepted += 1
        self._live.append(connection)
        try:
            for message in connection:
                self.received.append(message)
                connection.send(json.dumps({"echoed": json.loads(message)}))
        except Exception:
            # The server end closing mid-iteration is exactly what these tests
            # arrange, and it is not a failure of the server.
            pass
        finally:
            if connection in self._live:
                self._live.remove(connection)


@pytest.fixture
def server():
    with ServerUnderTest() as running:
        yield running


# How long to wait for the server thread to record a frame the client has already
# sent. Not a guess about speed: the client's send has returned, so this only
# covers the handoff between two threads on the same machine, and any real value
# is orders of magnitude below it. Waiting on the condition rather than sleeping
# a fixed time is what keeps these assertions from being a race.
SERVER_HANDOFF_TIMEOUT_SECONDS = 5.0
SERVER_HANDOFF_POLL_SECONDS = 0.005


def wait_until(condition, description: str) -> None:
    """Block until the server thread has caught up, or fail saying what did not happen."""
    deadline = time.monotonic() + SERVER_HANDOFF_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(SERVER_HANDOFF_POLL_SECONDS)
    raise AssertionError(f"the server never {description}")


def frames_with(server, predicate):
    return [message for message in list(server.received) if predicate(json.loads(message))]


def local_opener(server):
    """Open against the local server instead of the venue, keeping everything else real."""
    from websockets.sync.client import connect

    def open_websocket(_venue_url: str):
        return connect(server.url, open_timeout=5, close_timeout=1)

    return open_websocket


DEFAULT_REQUESTS = object()


def build_connection(adapter, server, clock, requests=DEFAULT_REQUESTS, **overrides):
    if requests is DEFAULT_REQUESTS:
        requests = [
            StreamRequest(StreamKind.TRADE, A_SYMBOL),
            StreamRequest(StreamKind.CANDLE, A_SYMBOL, candle_interval=A_CANDLE_INTERVAL),
        ]
    return VenueStreamConnection(
        adapter=adapter,
        requests=requests,
        reconnect_backoff_floor_seconds=overrides.get("floor", BACKOFF_FLOOR),
        reconnect_backoff_ceiling_seconds=overrides.get("ceiling", BACKOFF_CEILING),
        drain_interval_seconds=overrides.get("drain", DRAIN_INTERVAL),
        open_websocket=local_opener(server),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.fixture
def binance():
    return load_venue_adapter("binance-usdm")


@pytest.fixture
def bybit():
    return load_venue_adapter("bybit-linear")


@pytest.fixture
def clock():
    return FakeClock()


def test_mixing_routed_paths_on_one_connection_is_refused(binance, server, clock):
    """The silent-failure trap, refused at construction rather than found on the tape.

    Binance's trade stream routes to /market and its book to /public. One
    connection carrying both would deliver only one half, stay open, and error
    nothing -- which is the hardest possible fault to notice.
    """
    with pytest.raises(ConnectionRefused) as refusal:
        build_connection(
            binance,
            server,
            clock,
            requests=[
                StreamRequest(StreamKind.TRADE, A_SYMBOL),
                StreamRequest(StreamKind.BOOK, A_SYMBOL, book_depth_levels=A_BOOK_DEPTH),
            ],
        )
    assert "/market" in str(refusal.value) and "/public" in str(refusal.value)


def test_the_same_two_streams_share_one_connection_on_the_other_venue(bybit, server, clock):
    """Bybit routes everything to one endpoint, and the shape carries that too."""
    connection = build_connection(
        bybit,
        server,
        clock,
        requests=[
            StreamRequest(StreamKind.TRADE, A_SYMBOL),
            StreamRequest(StreamKind.BOOK, A_SYMBOL, book_depth_levels=A_BOOK_DEPTH),
        ],
    )
    assert connection.url.endswith("/v5/public/linear")
    assert connection.topics == ("publicTrade.BTCUSDT", "orderbook.50.BTCUSDT")


def test_a_connection_with_no_subscriptions_is_refused(binance, server, clock):
    with pytest.raises(ConnectionRefused):
        build_connection(binance, server, clock, requests=[])


def test_more_topics_than_the_venue_carries_is_refused_not_truncated(bybit, server, clock):
    """A dropped topic is a symbol with no tape and no error anywhere."""
    too_many = [
        StreamRequest(StreamKind.TRADE, f"AVERYLONGSYMBOLNAME{index}USDT") for index in range(700)
    ]
    with pytest.raises(ConnectionRefused) as refusal:
        build_connection(bybit, server, clock, requests=too_many)
    assert "does not fit" in str(refusal.value)


def test_opening_subscribes_with_the_adapter_s_own_frame_as_text(binance, server, clock):
    """Sent as text: measured, Binance refuses the identical bytes as a binary frame."""
    connection = build_connection(binance, server, clock)
    with connection:
        wait_until(lambda: bool(server.received), "received a subscribe frame")
        connection.drain()
    frame = json.loads(server.received[0])
    assert frame["method"] == "SUBSCRIBE"
    assert frame["params"] == ["btcusdt@aggTrade", "btcusdt@kline_1m"]


def test_drain_returns_what_arrived_and_counts_it(binance, server, clock):
    connection = build_connection(binance, server, clock)
    with connection:
        # The clock is frozen, so the drain deadline never passes on its own --
        # the messages themselves are what ends it, one echo per subscribe.
        received = connection.drain()
    assert received, "nothing arrived from a server that echoes"
    assert connection.standing.messages_received == len(received)
    assert all(isinstance(message, bytes) for message in received)


def test_a_client_pinging_venue_is_pinged_on_its_own_interval(bybit, server, clock):
    """Bybit closes a connection that stops pinging, so this is an obligation."""
    connection = build_connection(bybit, server, clock, drain=0.0)
    with connection:
        connection.drain()
        clock.now += bybit.heartbeat_discipline().interval_seconds + 1
        connection.drain()
        wait_until(
            lambda: frames_with(server, lambda frame: frame.get("op") == "ping"),
            "received the ping this venue requires",
        )
    pings = frames_with(server, lambda frame: frame.get("op") == "ping")
    assert len(pings) == 1, f"expected exactly one due ping, saw {len(pings)}"


def test_a_venue_that_pings_us_is_never_pinged_by_us(binance, server, clock):
    connection = build_connection(binance, server, clock, drain=0.0)
    with connection:
        connection.drain()
        clock.now += 3600
        connection.drain()
        # The subscribe frame is the proof the server thread has caught up, so an
        # absent ping here is an absence rather than a message still in flight.
        wait_until(lambda: bool(server.received), "received the subscribe frame")
    assert not frames_with(server, lambda frame: frame.get("op") == "ping")


def test_an_unexpected_close_backs_off_and_reconnects(binance, server, clock):
    """The floor, then doubling. Both are what keeps an outage from becoming a ban."""
    connection = build_connection(binance, server, clock)
    with connection:
        connection.drain()
        opened_before = connection.standing.opened_count
        server.close_live_connections()
        connection.drain()

    assert connection.standing.unexpected_close_count == 1
    assert connection.standing.opened_count > opened_before, "it did not reconnect"
    assert clock.slept == [BACKOFF_FLOOR]
    assert connection.standing.backed_off_seconds == BACKOFF_FLOOR


def test_the_backoff_doubles_and_stops_at_the_ceiling(binance, server, clock):
    connection = build_connection(binance, server, clock)
    waits = [connection._backoff_seconds(failures) for failures in range(1, 10)]
    assert waits[:4] == [1.0, 2.0, 4.0, 8.0]
    assert waits[-1] == BACKOFF_CEILING
    assert max(waits) == BACKOFF_CEILING


def test_a_ceiling_below_the_floor_is_refused(binance, server, clock):
    """It would make every wait the ceiling -- a disabled backoff that looks configured."""
    with pytest.raises(ConnectionRefused):
        build_connection(binance, server, clock, floor=10.0, ceiling=1.0)


def test_a_close_at_the_venue_s_stated_lifetime_is_routine_not_a_failure(binance, server, clock):
    """Binance disconnects every connection at 24 hours, and that is not an error.

    A reader that counted it would carry a backoff that grew every single day
    while nothing was wrong -- and would eventually be waiting at its ceiling
    against a venue that was never down.
    """
    connection = build_connection(binance, server, clock)
    with connection:
        connection.drain()
        clock.now += binance.connection_discipline().lifetime_seconds
        server.close_live_connections()
        connection.drain()

    assert connection.standing.scheduled_close_count == 1
    assert connection.standing.unexpected_close_count == 0
    assert connection.standing.consecutive_failures == 0
    assert clock.slept == [], "a scheduled close should cost no backoff at all"


def test_a_close_before_the_lifetime_is_not_read_as_scheduled(binance, server, clock):
    """Age alone is what separates the two, and the young connection failed."""
    connection = build_connection(binance, server, clock)
    with connection:
        connection.drain()
        clock.now += binance.connection_discipline().lifetime_seconds / 2
        server.close_live_connections()
        connection.drain()
    assert connection.standing.scheduled_close_count == 0
    assert connection.standing.unexpected_close_count == 1


def test_a_venue_that_states_no_lifetime_treats_every_close_as_unexpected(bybit, server, clock):
    """Bybit's idle cutoff is documented for private connections only.

    Claiming it here would make an unexpected close look scheduled, which is the
    one reading that would stop it being investigated.
    """
    assert bybit.connection_discipline().lifetime_seconds is None
    connection = build_connection(bybit, server, clock, requests=[StreamRequest(StreamKind.TRADE, A_SYMBOL)])
    with connection:
        connection.drain()
        clock.now += 86_400
        server.close_live_connections()
        connection.drain()
    assert connection.standing.scheduled_close_count == 0
    assert connection.standing.unexpected_close_count == 1


def test_the_connection_rate_window_waits_only_when_the_venue_states_one():
    """Binance publishes no futures connection rate, and an unstated limit is not none."""
    unstated = ConnectionRateWindow(limit=None, window_seconds=None)
    for _ in range(1000):
        assert unstated.seconds_until_room(now=0.0) == 0.0
        unstated.record_opened(now=0.0)

    stated = ConnectionRateWindow(limit=2, window_seconds=300.0)
    assert stated.seconds_until_room(now=0.0) == 0.0
    stated.record_opened(now=0.0)
    stated.record_opened(now=10.0)
    assert stated.seconds_until_room(now=20.0) == 280.0
    # Once the oldest falls out of the window there is room again, without anyone
    # having to reset a counter.
    assert stated.seconds_until_room(now=301.0) == 0.0


def test_reconnecting_waits_for_room_under_the_venue_s_stated_rate(bybit, server, clock):
    """Bybit's 500-per-5-minutes is what a reconnect storm actually breaks."""
    connection = build_connection(bybit, server, clock, requests=[StreamRequest(StreamKind.TRADE, A_SYMBOL)])
    limit = bybit.connection_discipline().new_connections_per_window
    window = bybit.connection_discipline().rate_window_seconds
    connection._rate_window.opened_at = [clock.now - 1.0] * limit

    connection.open()
    assert connection.standing.waited_for_connection_budget_seconds > 0
    assert connection.standing.waited_for_connection_budget_seconds <= window
    connection.close()


def test_a_failed_reconnect_is_counted_rather_than_raised(binance, clock):
    """A part that died on a refused reconnect would stay dead until noticed."""

    def refuse_to_open(_url):
        raise OSError("connection refused")

    connection = VenueStreamConnection(
        adapter=binance,
        requests=[StreamRequest(StreamKind.TRADE, A_SYMBOL)],
        reconnect_backoff_floor_seconds=BACKOFF_FLOOR,
        reconnect_backoff_ceiling_seconds=BACKOFF_CEILING,
        drain_interval_seconds=DRAIN_INTERVAL,
        open_websocket=refuse_to_open,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with pytest.raises(OSError):
        connection.open()

    # But a refusal *during* recovery is counted, not raised -- the loop lives.
    connection._handle_close("closed by peer", close_code=1006)
    assert connection.standing.unexpected_close_count == 1
    assert connection.standing.consecutive_failures == 2
    assert "connection refused" in connection.standing.last_close_reason


def test_closing_releases_the_socket(binance, server, clock):
    """The suite runs under -W error::ResourceWarning, and a stream reader owns sockets."""
    connection = build_connection(binance, server, clock)
    connection.open()
    assert connection.standing.is_open
    connection.close()
    assert not connection.standing.is_open
    # Closing twice is not an error: a part is SIGKILLed as the ordinary way of
    # being switched off, so every teardown path has to be safe to repeat.
    connection.close()
