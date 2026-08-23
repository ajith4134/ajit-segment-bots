"""One digest over what a board snapshot shows, shared by the publisher and the watch.

Built-at is excluded deliberately: a snapshot rebuilt with identical content is
the same board, and a digest that included the time would make every rebuild
look like a change and hide the ones that really were. The publisher stamps
this digest on the link it publishes and the stale-board watch recomputes it
from the snapshot it saw; two definitions would drift, and the watch would
alert forever on a board that was never stale.
"""

from __future__ import annotations

import hashlib


def board_digest(snapshot) -> str:
    body = "|".join(
        f"{tile.label}:{tile.state}:{tile.value}:{tile.proof}" for tile in snapshot.tiles
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = ["board_digest"]
