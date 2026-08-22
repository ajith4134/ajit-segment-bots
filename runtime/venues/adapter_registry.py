"""Resolve the venue adapters named in settings, so no part ever names a venue.

Adding a venue is: write `runtime/venues/<venue_id with underscores>.py` exposing
`build_venue_adapter()`, then add its id to the `captured_venues` setting. That
is the whole procedure the user's condition asked for -- *"mark it so if the 2
are not enough we can add more later"* -- and it is enforced rather than
documented, because the conformance test enumerates whatever this returns.

The mapping from id to module is a convention, not a table. A table would be a
third place to edit and a fourth place to forget: a venue whose module exists and
whose settings line exists but whose registry line does not would look, from
outside, exactly like a venue that was never added.
"""

from __future__ import annotations

import importlib
from typing import Sequence

from runtime.settings_reader import SettingsDocument
from runtime.venues.venue_adapter import VenueAdapter

# The setting listing which venues are captured right now (spec section 4.3).
CAPTURED_VENUES_SETTING = "captured_venues"

# The package every venue module lives in, and the factory each one exposes.
VENUE_PACKAGE = "runtime.venues"
ADAPTER_FACTORY_NAME = "build_venue_adapter"


class VenueAdapterMissing(LookupError):
    """A venue named in settings has no adapter, or one that is not the shape."""


def module_name_for_venue(venue_id: str) -> str:
    """The module a venue id resolves to. `binance-usdm` -> `binance_usdm`."""
    return venue_id.replace("-", "_")


def load_venue_adapter(venue_id: str) -> VenueAdapter:
    """Build the adapter for one venue id, refusing anything that is not one.

    Every failure raises the same exception on purpose: from a caller's point of
    view "no module", "no factory" and "not a VenueAdapter" are one fact -- this
    venue cannot be captured -- and the message says which of them it was.
    """
    module_path = f"{VENUE_PACKAGE}.{module_name_for_venue(venue_id)}"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as failure:
        raise VenueAdapterMissing(
            f"venue '{venue_id}' is named in settings but {module_path} does not exist. "
            f"Adding a venue is a module here plus that settings line -- never an edit to a part."
        ) from failure

    factory = getattr(module, ADAPTER_FACTORY_NAME, None)
    if factory is None:
        raise VenueAdapterMissing(
            f"{module_path} exposes no {ADAPTER_FACTORY_NAME}(). Every venue module is the "
            f"same shape (T-1), so the registry never has to know which one it is looking at."
        )

    adapter = factory()
    if not isinstance(adapter, VenueAdapter):
        raise VenueAdapterMissing(
            f"{module_path}.{ADAPTER_FACTORY_NAME}() returned {type(adapter).__name__}, "
            f"which is not a VenueAdapter. A venue that answers a different question set "
            f"would push its oddities back out into the parts."
        )
    if adapter.venue_id != venue_id:
        raise VenueAdapterMissing(
            f"{module_path} builds an adapter calling itself '{adapter.venue_id}' while "
            f"settings and the tape call it '{venue_id}'. The tape is written under the "
            f"venue id, so a disagreement here splits one venue's capture across two names."
        )
    return adapter


def read_captured_venue_ids(settings: SettingsDocument) -> tuple[str, ...]:
    """The venue ids the operator has turned on, in the order they were written."""
    value = settings.read_value(CAPTURED_VENUES_SETTING)
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(
            f"'{CAPTURED_VENUES_SETTING}' in {settings.source_path} is {value!r}, not a list of "
            f"venue ids. One venue written as a bare string would be captured as a set of "
            f"single-character venues."
        )
    return tuple(str(venue_id) for venue_id in value)


def load_captured_venue_adapters(settings: SettingsDocument) -> tuple[VenueAdapter, ...]:
    """Every adapter the operator has turned on, built and checked to be one."""
    return tuple(load_venue_adapter(venue_id) for venue_id in read_captured_venue_ids(settings))
