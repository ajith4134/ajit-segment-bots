"""One websocket connection to one venue, kept alive the way that venue expects.

Synchronous on purpose. A part's loop is `run_part`: it selects on the control
socket, then calls `do_one_tick()` (phase 0 §4). An asynchronous client would
need its own event loop and a thread to run it in, and a part that could only be
switched off between awaits is a part the governor cannot switch -- T-2 lost for
no gain, since one connection blocked on one socket is exactly what a tick is.

It is built from `StreamRequest`s rather than from topic strings. The difference
matters: given strings, this module would have to work out which venue stream
each one is in order to know which endpoint it routes to, which is venue parsing
living outside the venue module. Given requests, it asks the adapter both
questions and parses nothing.

What is venue-specific stays in the adapter, and nothing here duplicates it:

- **The routed path** comes from `stream_endpoint_url`. Binance's `/market` and
  `/public` streams cannot share a connection at all -- the wrongly-routed half
  stays open and silent rather than erroring -- so a mixed connection is refused
  here instead of discovered as an empty tape.
- **How much fits** comes from `does_topic_fit_connection`, so an over-full
  connection is refused rather than silently truncated by the venue.
- **Who pings** comes from `heartbeat_discipline`. Binance pings us and the
  library answers; Bybit closes a connection that stops sending it an
  application-level ping every 20 seconds.
- **A scheduled close is not a fault.** `connection_discipline` states the
  lifetime, and Binance closes every connection at 24 hours. A reader that
  treated that as an error would double its backoff every day for no reason.
- **Reconnecting can itself be the harm.** Bybit allows 500 new connections per
  IP per rolling 5 minutes, so a reconnect storm against an outage produces a ban
  that outlasts the outage. Where a venue states a rate, this waits for room
  rather than racing it.

Everything the connection knows about itself is counted and reported --
`ConnectionStanding` -- because from the tape alone, a stream that is quietly
reconnecting every second looks exactly like a quiet market.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as connect_websocket

from runtime.venues.venue_adapter import StreamRequest, VenueAdapter

# Protocol constants for a deliberate close by either end, not numbers this
# project chose. Both venues use them for a scheduled disconnect.
NORMAL_CLOSURE_CODE = 1000
GOING_AWAY_CODE = 1001

# The first wait after a failure is the floor and each further one doubles. Two
# is the arithmetic of doubling rather than a tunable.
BACKOFF_DOUBLING_BASE = 2


class ConnectionRefused(ValueError):
    """A connection was asked for that the venue could not serve as described."""


@dataclass
class ConnectionStanding:
    """What this connection has actually done, so nothing about it is inferred.

    A tape cannot tell a quiet market from a connection reconnecting every
    second, and neither can it tell either of those from a connection opened to
    the wrong routed path. These counts are what makes the difference visible,
    and they are what the part reports as its own health.
    """

    venue_id: str
    opened_count: int = 0
    scheduled_close_count: int = 0
    unexpected_close_count: int = 0
    consecutive_failures: int = 0
    waited_for_connection_budget_seconds: float = 0.0
    backed_off_seconds: float = 0.0
    messages_received: int = 0
    last_close_reason: str | None = None
    connected_since_monotonic: float | None = None

    @property
    def is_open(self) -> bool:
        return self.connected_since_monotonic is not None


@dataclass
class ConnectionRateWindow:
    """When connections were opened, so a venue's stated rate can be honoured.

    Per process, and that is a real limit rather than an oversight. Both venues
    count per IP, so several capture processes on this box share one budget none
    of them can see. A cross-process budget belongs with the governor, which
    already owns everything scarce across parts; carrying it here would mean two
    answers to the same question with no way to tell which was authoritative.
    """

    limit: int | None
    window_seconds: float | None
    opened_at: list[float] = field(default_factory=list)

    def seconds_until_room(self, now: float) -> float:
        """How long to wait before opening one more without breaking the rate.

        Zero when the venue states no rate -- which is not "no wait is needed"
        but "this venue publishes nothing to wait against", leaving the backoff
        as the only guard. Binance is that case: no connections-per-IP figure
        exists in its futures documentation at all.
        """
        if self.limit is None or self.window_seconds is None:
            return 0.0
        self.opened_at = [at for at in self.opened_at if now - at < self.window_seconds]
        if len(self.opened_at) < self.limit:
            return 0.0
        return max(0.0, self.window_seconds - (now - min(self.opened_at)))

    def record_opened(self, now: float) -> None:
        self.opened_at.append(now)


class VenueStreamConnection:
    """A subscribed connection that reopens itself, at the pace the venue allows."""

    def __init__(
        self,
        adapter: VenueAdapter,
        requests: Sequence[StreamRequest],
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
        open_websocket: Callable[..., object] = connect_websocket,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not requests:
            raise ConnectionRefused(
                f"{adapter.venue_id}: a connection with no subscriptions would open, stay open, "
                f"and deliver nothing -- which is indistinguishable from the routed-path fault "
                f"this whole design exists to catch"
            )
        if reconnect_backoff_ceiling_seconds < reconnect_backoff_floor_seconds:
            raise ConnectionRefused(
                f"reconnect backoff ceiling {reconnect_backoff_ceiling_seconds} is below its "
                f"floor {reconnect_backoff_floor_seconds}; every wait would be the ceiling, "
                f"which disables the backoff while looking like it is configured"
            )

        self._adapter = adapter
        self._requests = list(requests)
        self._url = _one_route_for(adapter, self._requests)
        self._topics = _topics_that_fit(adapter, self._requests)
        self._floor = reconnect_backoff_floor_seconds
        self._ceiling = reconnect_backoff_ceiling_seconds
        self._drain_interval = drain_interval_seconds
        self._open_websocket = open_websocket
        self._monotonic = monotonic
        self._sleep = sleep

        discipline = adapter.connection_discipline()
        self._lifetime_seconds = discipline.lifetime_seconds
        self._rate_window = ConnectionRateWindow(
            limit=discipline.new_connections_per_window,
            window_seconds=discipline.rate_window_seconds,
        )
        self._heartbeat = adapter.heartbeat_discipline()
        self._connection = None
        self._last_ping_at: float | None = None
        self.standing = ConnectionStanding(venue_id=adapter.venue_id)

    @property
    def url(self) -> str:
        """Where this connection points, routed path included."""
        return self._url

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(self._topics)

    def open(self) -> None:
        """Connect and subscribe, waiting first for room under the venue's rate."""
        self._close_socket()
        wait = self._rate_window.seconds_until_room(self._monotonic())
        if wait > 0:
            self.standing.waited_for_connection_budget_seconds += wait
            self._sleep(wait)

        now = self._monotonic()
        self._connection = self._open_websocket(self._url)
        self._rate_window.record_opened(now)
        self.standing.opened_count += 1
        self.standing.connected_since_monotonic = now
        self._last_ping_at = now
        # Sent as text, not as the bytes the adapter returns. Measured 2026-08-22:
        # Binance closes the connection with 1008 "Invalid request" on a binary
        # frame carrying exactly the same JSON, and the library picks the frame
        # type from the argument's type -- so this decode is load-bearing.
        self._connection.send(self._adapter.subscribe_frame(self._topics).decode("utf-8"))

    def drain(self) -> list[bytes]:
        """Every message that arrived within this tick, as the bytes the venue sent.

        Returns a list rather than yielding: the caller writes these to the tape,
        and a generator would let a caller abandon the connection mid-drain with
        messages received and never written.
        """
        if self._connection is None:
            self.open()

        deadline = self._monotonic() + self._drain_interval
        received: list[bytes] = []
        while True:
            # The heartbeat comes before the deadline check, not after it. A tick
            # with no time left still owes the venue its ping, and a reader that
            # skipped it whenever it was busy would be closed for silence at
            # exactly the moment it had the most to capture.
            try:
                # Sending discovers a dead connection as readily as receiving
                # does, and on a venue we ping it usually gets there first: the
                # socket closes between ticks and the next heartbeat is what
                # finds out. Guarded so that discovery reconnects rather than
                # ending the part.
                self._send_heartbeat_if_due()
            except ConnectionClosed as closure:
                code = closure.rcvd.code if closure.rcvd is not None else None
                self._handle_close(str(closure), code)
                break
            except WebSocketException as failure:
                self._handle_close(f"{type(failure).__name__}: {failure}", None)
                break

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            try:
                message = self._connection.recv(timeout=remaining)
            except TimeoutError:
                break
            except ConnectionClosed as closure:
                code = closure.rcvd.code if closure.rcvd is not None else None
                self._handle_close(str(closure), code)
                break
            except WebSocketException as failure:
                self._handle_close(f"{type(failure).__name__}: {failure}", None)
                break
            received.append(message.encode("utf-8") if isinstance(message, str) else message)

        self.standing.messages_received += len(received)
        return received

    def _send_heartbeat_if_due(self) -> None:
        """The application-level ping this venue expects, if it expects one.

        Distinct from the library's own protocol-level keepalive, which detects a
        half-open TCP connection. Bybit's ping is an application message it
        answers with `ret_msg: "pong"`, and a connection that stops sending it is
        closed for a reason that reads exactly like a network fault.
        """
        if not self._heartbeat.expects_client_ping or self._connection is None:
            return
        now = self._monotonic()
        if self._last_ping_at is not None and now - self._last_ping_at < self._heartbeat.interval_seconds:
            return
        self._connection.send(self._heartbeat.ping_frame.decode("utf-8"))
        self._last_ping_at = now

    def _handle_close(self, reason: str, close_code: int | None) -> None:
        """Record the close, and reopen -- unless the venue scheduled it.

        A scheduled close resets the failure count, which is the whole reason to
        ask the adapter for a lifetime: Binance closes every connection at 24
        hours, and a reader counting that as a failure would carry a backoff that
        grew every day while nothing at all was wrong.
        """
        age = self._connection_age()
        self._close_socket()
        self.standing.last_close_reason = reason

        if self._was_scheduled(age, close_code):
            self.standing.scheduled_close_count += 1
            self.standing.consecutive_failures = 0
            self._reopen_or_count_failure()
            return

        self.standing.unexpected_close_count += 1
        self.standing.consecutive_failures += 1
        wait = self._backoff_seconds(self.standing.consecutive_failures)
        self.standing.backed_off_seconds += wait
        self._sleep(wait)
        self._reopen_or_count_failure()

    def _reopen_or_count_failure(self) -> None:
        """Try to come back, and treat a refusal as one more failure, not a crash.

        A stream reader that died on a refused reconnect would stay dead until
        the governor noticed a missing process. Counting it keeps the part's loop
        alive, keeps it answering the control channel, and makes the next attempt
        wait longer -- and the count is what the board reads.
        """
        try:
            self.open()
        except (OSError, WebSocketException) as failure:
            self.standing.last_close_reason = f"{type(failure).__name__}: {failure}"
            self.standing.consecutive_failures += 1

    def _was_scheduled(self, age: float | None, close_code: int | None) -> bool:
        """Whether this close is the one the venue told us to expect.

        Both conditions must hold. A normal close code alone is not enough -- a
        venue restarting a node closes normally too -- and age alone is not
        enough either, since a connection can die of the network at any age.
        """
        if self._lifetime_seconds is None or age is None:
            return False
        if close_code not in (NORMAL_CLOSURE_CODE, GOING_AWAY_CODE, None):
            return False
        return age >= self._lifetime_seconds

    def _connection_age(self) -> float | None:
        if self.standing.connected_since_monotonic is None:
            return None
        return self._monotonic() - self.standing.connected_since_monotonic

    def _backoff_seconds(self, consecutive_failures: int) -> float:
        """Floor, doubling, capped.

        The floor exists because Bybit's 500-per-5-minutes makes an eager retry
        the thing that turns an outage into a ban; the ceiling exists because an
        unbounded doubling is a connection that has silently stopped trying.
        """
        exponent = max(0, consecutive_failures - 1)
        return min(self._floor * (BACKOFF_DOUBLING_BASE**exponent), self._ceiling)

    def _close_socket(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except (OSError, WebSocketException):
                # Already gone. Closing a dead socket is not a fault worth
                # reporting; what matters is that the handle is released, and why
                # the connection ended was recorded before this was called.
                pass
            self._connection = None
        self.standing.connected_since_monotonic = None

    def close(self) -> None:
        self._close_socket()

    def __enter__(self) -> "VenueStreamConnection":
        self.open()
        return self

    def __exit__(self, *exception) -> None:
        self.close()


def _one_route_for(adapter: VenueAdapter, requests: Sequence[StreamRequest]) -> str:
    """The single endpoint every request on this connection shares, or a refusal."""
    urls = {adapter.stream_endpoint_url(request.stream_kind) for request in requests}
    if len(urls) != 1:
        raise ConnectionRefused(
            f"{adapter.venue_id}: these subscriptions route to {sorted(urls)}. One connection "
            f"carries one route -- on this venue the wrongly-routed half would stay open and "
            f"deliver nothing rather than failing, which is the hardest fault to see."
        )
    return urls.pop()


def _topics_that_fit(adapter: VenueAdapter, requests: Sequence[StreamRequest]) -> list[str]:
    """Phrase every request, refusing the first one the venue could not also carry.

    The adapter answers whether one more fits, because the arithmetic differs per
    venue -- a stream count on one, a character count of the serialised subscribe
    payload on the other. Refusing here rather than truncating means an oversized
    connection is a startup error instead of a tape that is quietly missing the
    symbols at the end of the list.
    """
    topics: list[str] = []
    for request in requests:
        topic = adapter.subscription_topic(request)
        if not adapter.does_topic_fit_connection(topics, topic):
            raise ConnectionRefused(
                f"{adapter.venue_id}: {topic} does not fit a connection already carrying "
                f"{len(topics)} subscriptions. Split them across connections -- silently "
                f"dropping it would leave a symbol with no tape and no error."
            )
        topics.append(topic)
    return topics
