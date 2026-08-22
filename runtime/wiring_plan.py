"""Turn the blueprint into addresses: who listens where, and where a producer sends.

R-01 says a part declares data and never another part, and that two parts are
connected when one produces what the other consumes. This module is that sentence
executed: it reads docs/features.json and computes, for every part, the inbox it
binds per consumed type and the addresses it sends to per produced type.

There is deliberately no file in which an edge can be written by hand. A wire that
the blueprint does not imply cannot be created here, and a part cannot enumerate
its peers because it is handed addresses, never names (T-4).

Three rules the derivation enforces rather than assumes:

  1. A part never receives its own message. Fifteen parts consume a type they also
     produce -- the eleven part-health readers among them, since every part emits
     health and those eleven read it.
  2. Peer blocks never wire into each other (R-03). check_contracts.py proves no
     such type exists; this asserts it again at the point where the wire would
     actually be created, because a rule checked only where it was written down is
     a rule that survives exactly until someone adds a second path.
  3. An address that would exceed the kernel's sun_path limit is refused by name.
     The longest the current blueprint produces is 92 of the 107 usable bytes, and
     that headroom is the reason inboxes sit directly under the project's runtime
     directory rather than in an "inboxes" subdirectory below it: the subdirectory
     cost eight of the fifteen bytes left. It must fail loudly rather than by
     silent truncation onto another part's inbox.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass

from runtime.part_declaration import (
    BLUEPRINT_PATH,
    PartDeclaration,
    RateRisk,
    ResourceClass,
    SkippedTickEffect,
)

# struct sockaddr_un.sun_path is 108 bytes on Linux, and the path must be
# NUL-terminated, so 107 bytes are usable. This is a wire-format width, not a
# decision (RL-061): it is the kernel's number, not a choice anyone may tune.
SUN_PATH_USABLE_BYTES = 107

# Where inboxes live. XDG_RUNTIME_DIR is per-user, mode 0700, and the kernel
# enforces it -- which is the only reason a pickled payload may be read off this
# bus at all (spec section 2.2). The two decisions are one decision.
INBOX_DIRECTORY_NAME = "ajit-segment-bots"
INBOX_DIRECTORY_MODE = 0o700


class InboxAddressTooLong(ValueError):
    """An address would not fit in sun_path, so the wire cannot be created."""


class PeerBlocksWired(ValueError):
    """A derived wire crosses between peer blocks, which R-03 forbids."""


@dataclass(frozen=True)
class PartWiring:
    """Every address one part needs, and nothing about any other part.

    inboxes maps a consumed data type to the address this part binds for it.
    outbound maps a produced data type to the addresses this part sends to. The
    part sees callables built over these, never the mapping itself -- what it can
    reach is decided here, once, from the blueprint.
    """

    part_id: str
    declaration: PartDeclaration
    inboxes: dict[str, pathlib.Path]
    outbound: dict[str, tuple[pathlib.Path, ...]]

    def consumer_count(self, data_type: str) -> int:
        return len(self.outbound.get(data_type, ()))

    def has_no_listeners(self, data_type: str) -> bool:
        """True when nothing in the blueprint consumes this produced type.

        Not an error here: check_contracts.py already refuses an orphan output at
        the blueprint, so reaching this means the blueprint changed underneath.
        """
        return self.consumer_count(data_type) == 0


def inbox_root(runtime_directory: pathlib.Path | None = None) -> pathlib.Path:
    """The directory every inbox address sits in.

    Defaults to the project's directory under XDG_RUNTIME_DIR, which systemd
    creates per-user at mode 0700 and removes at logout.

    A caller may pass its own root, and it is then used verbatim rather than having
    the project directory appended: every byte of the path competes with the part id
    and data type for the kernel's 107, and a test root that nested one directory
    deeper would fail on address length instead of on what it meant to check.
    """
    if runtime_directory is not None:
        return runtime_directory
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_root:
        raise RuntimeError(
            "XDG_RUNTIME_DIR is unset, so there is no per-user directory the kernel "
            "keeps private. The bus will not fall back to a world-reachable address: "
            "every message on it is deserialised by the part that receives it."
        )
    return pathlib.Path(runtime_root) / INBOX_DIRECTORY_NAME


def create_inbox_root(runtime_directory: pathlib.Path | None = None) -> pathlib.Path:
    """Make the inbox directory exist and be private. Returns it."""
    root = inbox_root(runtime_directory)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(INBOX_DIRECTORY_MODE)
    return root


def inbox_address(part_id: str, data_type: str, runtime_directory: pathlib.Path | None = None) -> pathlib.Path:
    """Where `part_id` receives `data_type`.

    Refuses an address longer than sun_path allows, naming both halves: a
    truncated path would silently become some other part's inbox, which is the
    worst failure this bus could have -- data delivered, to the wrong part, with
    nothing reporting an error.
    """
    address = inbox_root(runtime_directory) / f"{part_id}.{data_type}"
    encoded = len(str(address).encode())
    if encoded > SUN_PATH_USABLE_BYTES:
        raise InboxAddressTooLong(
            f"the inbox address for part '{part_id}' and data type '{data_type}' is "
            f"{encoded} bytes, and a unix socket path may be at most "
            f"{SUN_PATH_USABLE_BYTES}. Shorten the part id or the data type in the "
            f"blueprint -- a truncated address would bind to another part's inbox and "
            f"deliver data to the wrong part without any error being raised."
        )
    return address


def _declaration_of(feature: dict) -> PartDeclaration:
    return PartDeclaration(
        part_id=feature["id"],
        consumes=tuple(feature["consumes"]),
        produces=tuple(feature["produces"]),
        resource_class=ResourceClass(feature["resource_class"]),
        rate_risk=RateRisk(feature["rate_risk"]),
        skipped_tick_effect=SkippedTickEffect(feature["skipped_tick_effect"]),
    )


def load_blueprint(blueprint_path: pathlib.Path = BLUEPRINT_PATH) -> dict:
    return json.loads(blueprint_path.read_text())


def derive_wiring(
    blueprint: dict | None = None,
    runtime_directory: pathlib.Path | None = None,
) -> dict[str, PartWiring]:
    """Compute every part's addresses from the blueprint. Nothing else may.

    The result is a plan, not a connection: no socket is created here, and a part
    whose process is not running simply has no one bound at its address, which is
    what makes an off part detectable (spec section 5).
    """
    registry = blueprint if blueprint is not None else load_blueprint()
    features = registry["features"]
    peer_group_of_category = {
        category["id"]: category.get("peer_group") for category in registry.get("categories", [])
    }
    category_of_part = {feature["id"]: feature["category"] for feature in features}

    consumers_of_type: dict[str, list[str]] = {}
    for feature in features:
        for data_type in feature["consumes"]:
            consumers_of_type.setdefault(data_type, []).append(feature["id"])

    wiring: dict[str, PartWiring] = {}
    for feature in features:
        part_id = feature["id"]
        inboxes = {
            data_type: inbox_address(part_id, data_type, runtime_directory)
            for data_type in feature["consumes"]
        }
        outbound: dict[str, tuple[pathlib.Path, ...]] = {}
        for data_type in feature["produces"]:
            addresses = []
            for consumer_id in consumers_of_type.get(data_type, []):
                if consumer_id == part_id:
                    continue  # rule 1: a part never receives its own message
                _refuse_peer_wire(
                    producer_id=part_id,
                    consumer_id=consumer_id,
                    data_type=data_type,
                    category_of_part=category_of_part,
                    peer_group_of_category=peer_group_of_category,
                )
                addresses.append(inbox_address(consumer_id, data_type, runtime_directory))
            outbound[data_type] = tuple(addresses)
        wiring[part_id] = PartWiring(
            part_id=part_id,
            declaration=_declaration_of(feature),
            inboxes=inboxes,
            outbound=outbound,
        )
    return wiring


def _refuse_peer_wire(
    producer_id: str,
    consumer_id: str,
    data_type: str,
    category_of_part: dict[str, str],
    peer_group_of_category: dict[str, str | None],
) -> None:
    """R-03, asserted where the wire would be made rather than only where it is drawn."""
    producer_category = category_of_part.get(producer_id)
    consumer_category = category_of_part.get(consumer_id)
    if producer_category == consumer_category:
        return
    producer_group = peer_group_of_category.get(producer_category)
    if producer_group and producer_group == peer_group_of_category.get(consumer_category):
        raise PeerBlocksWired(
            f"R-03: '{producer_id}' in block '{producer_category}' would send "
            f"'{data_type}' to '{consumer_id}' in block '{consumer_category}'. Both "
            f"belong to peer group '{producer_group}', and peer blocks are copies of "
            f"one idea running separately -- they never wire into each other. The fix "
            f"is a distinct data type per peer, not an exception here."
        )


def derive_edges_from_wiring(wiring: dict[str, PartWiring]) -> set[tuple[str, str, str]]:
    """(producer, consumer, data type) for every wire this plan would create.

    Exists so the plan can be compared against the blueprint's own edge derivation:
    two independent computations of the same set, and a difference between them is
    a defect in one of them rather than a matter of opinion.
    """
    edges = set()
    consumer_of_address: dict[pathlib.Path, str] = {}
    for part in wiring.values():
        for data_type, address in part.inboxes.items():
            consumer_of_address[address] = part.part_id
    for part in wiring.values():
        for data_type, addresses in part.outbound.items():
            for address in addresses:
                edges.add((part.part_id, consumer_of_address[address], data_type))
    return edges
