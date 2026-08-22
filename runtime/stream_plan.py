"""`stream-plan`: which subscriptions ride which connection, on which venue.

The data type `stream-budget-planner` produces and every streaming reader
consumes. It lives here rather than in either part because it is data, and under
T-4 a part names data and never another part -- a reader that imported the
planner to learn the plan's shape would be wired to the planner itself.

The plan is per connection rather than a flat list of subscriptions on purpose.
How many fit on one connection is a venue question with a different answer per
venue -- a stream count on Binance, a character count of the serialised subscribe
payload on Bybit -- so the assignment is made once, by the part that consumes
`hardware-capacity` and the adapters' own limits, and carried here as a decision
rather than recomputed by each reader.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.tape import StreamKind
from runtime.venues.venue_adapter import StreamRequest


@dataclass(frozen=True)
class ConnectionAssignment:
    """One connection's worth of subscriptions on one venue."""

    venue_id: str
    requests: tuple[StreamRequest, ...]

    def requests_of_kind(self, stream_kind: StreamKind) -> tuple[StreamRequest, ...]:
        return tuple(request for request in self.requests if request.stream_kind is stream_kind)


@dataclass(frozen=True)
class StreamPlan:
    """Every connection the capture is meant to be holding right now."""

    connections: tuple[ConnectionAssignment, ...]

    def assignments_for(self, venue_id: str, stream_kind: StreamKind) -> tuple[ConnectionAssignment, ...]:
        """This venue's connections that carry this kind, each narrowed to it.

        Narrowed rather than filtered whole: a connection may carry more than one
        stream kind on a venue that routes them together, and a reader must
        subscribe to its own kind only -- two readers subscribing to the same
        topic would write the same message to the tape twice.
        """
        narrowed = []
        for assignment in self.connections:
            if assignment.venue_id != venue_id:
                continue
            requests = assignment.requests_of_kind(stream_kind)
            if requests:
                narrowed.append(ConnectionAssignment(venue_id=venue_id, requests=requests))
        return tuple(narrowed)
