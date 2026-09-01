"""The test that keeps venue-independence honest (spec section 3.2).

It enumerates the venues named in settings, resolves each to an adapter, and puts
the whole question set to it. Adding OKX means writing `okx_swap.py` and adding
its id to `captured_venues`; this test then covers it with no edit here. If
adding a venue ever needs a part changed, the design has failed and this is where
that shows up.

The questions that need a real venue message are checked to be *answered* rather
than *inherited*, and are exercised against real payloads in each venue's own
tests -- RL-063 forbids inventing a message and calling the result a test.
"""

import pathlib

import pytest

from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import TradeFidelity
from runtime.venues.adapter_registry import (
    ADAPTER_FACTORY_NAME,
    CAPTURED_VENUES_SETTING,
    VenueAdapterMissing,
    load_venue_adapter,
    module_name_for_venue,
    read_captured_venue_ids,
)
from runtime.venues.venue_adapter import (
    CRYPTO_STREAM_KINDS,
    QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE,
    QUESTIONS_ANSWERED_WITHOUT_VENUE_DATA,
    SequenceContinuity,
    StreamRequest,
    VenueAdapter,
    VenueFact,
    VenueFactWithoutSource,
)

PROJECT = pathlib.Path(__file__).resolve().parents[3]
EXAMPLE_SETTINGS = PROJECT / "settings" / "runtime.example.toml"
LIVE_SETTINGS = settings_directory() / "runtime.toml"

# A symbol and an interval to phrase a subscription with. Not market data -- these
# only have to be a well-formed symbol and a well-formed interval for the adapter
# to phrase something; what each venue's phrasing actually is gets tested against
# that venue's own documented format in its own test module.
A_SYMBOL = "BTCUSDT"
A_CANDLE_INTERVAL = "1m"
A_BOOK_DEPTH = 20


def captured_venue_ids_from(path: pathlib.Path) -> tuple[str, ...]:
    return read_captured_venue_ids(load_settings_document(path, "runtime"))


def ask_every_question_answerable_without_venue_data(adapter: VenueAdapter) -> None:
    """Put the whole no-data half of the question set to one adapter."""
    assert isinstance(adapter.venue_id, str) and adapter.venue_id
    assert isinstance(adapter.trade_fidelity, TradeFidelity)

    limits = adapter.declared_limits()
    assert limits, f"{adapter.venue_id} declares no limits, so nothing can be sized against it"
    for name, fact in limits.items():
        assert isinstance(fact, VenueFact), f"{adapter.venue_id}'s '{name}' limit is not a VenueFact"
        assert fact.source.strip(), f"{adapter.venue_id}'s '{name}' limit cites no source"

    topics = []
    for stream_kind in CRYPTO_STREAM_KINDS:
        continuity = adapter.sequence_continuity(stream_kind)
        assert isinstance(continuity, SequenceContinuity), (
            f"{adapter.venue_id} does not say what its {stream_kind.name} sequence promises, "
            f"so §6 has nothing to check continuity against"
        )
        url = adapter.stream_endpoint_url(stream_kind)
        assert url.startswith("wss://"), f"{adapter.venue_id} {stream_kind.name} url is {url!r}"
        topic = adapter.subscription_topic(
            StreamRequest(
                stream_kind=stream_kind,
                symbol=A_SYMBOL,
                candle_interval=A_CANDLE_INTERVAL,
                book_depth_levels=A_BOOK_DEPTH,
            )
        )
        assert isinstance(topic, str) and topic
        topics.append(topic)

    # The adapter answers whether one more fits; the caller never counts anything.
    assert adapter.does_topic_fit_connection([], topics[0]) is True
    assert isinstance(adapter.does_topic_fit_connection(topics, topics[0]), bool)

    assert isinstance(adapter.subscribe_frame(topics), bytes)
    assert isinstance(adapter.unsubscribe_frame(topics), bytes)

    connection_rules = adapter.connection_discipline()
    # Every field may be None -- an unstated limit is not an absent one -- but a
    # venue that states a rate must also state the window it is counted over, or
    # nothing can wait against it.
    assert (connection_rules.new_connections_per_window is None) == (
        connection_rules.rate_window_seconds is None
    ), f"{adapter.venue_id} states a connection rate with no window, or a window with no rate"

    discipline = adapter.heartbeat_discipline()
    assert isinstance(discipline.expects_client_ping, bool)
    if discipline.expects_client_ping:
        assert discipline.interval_seconds, (
            f"{adapter.venue_id} expects the client to ping but names no interval, "
            f"which is a connection the venue closes for reasons that read as a network fault"
        )


def assert_data_questions_are_answered_not_inherited(adapter: VenueAdapter) -> None:
    """Every question needing a real message is implemented by this adapter itself."""
    for question in QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE:
        own = getattr(type(adapter), question, None)
        base = getattr(VenueAdapter, question, None)
        assert own is not None and own is not base, (
            f"{adapter.venue_id} inherits {question} instead of answering it"
        )


@pytest.mark.parametrize("settings_path", [EXAMPLE_SETTINGS, LIVE_SETTINGS], ids=["template", "live"])
def test_every_captured_venue_answers_the_whole_question_set(settings_path):
    """Whatever settings names must resolve and must answer everything.

    A venue id here with no module is a failure, not a skip: the tape is written
    per venue, so a venue that silently captures nothing is an empty directory
    that looks exactly like a quiet market.
    """
    if not settings_path.exists():
        pytest.skip(f"{settings_path} is not installed on this machine")
    venue_ids = captured_venue_ids_from(settings_path)
    if not venue_ids:
        pytest.skip(
            f"{CAPTURED_VENUES_SETTING} in {settings_path} is empty -- no adapter has landed "
            f"yet, so this question set has NOT been put to anything"
        )
    for venue_id in venue_ids:
        adapter = load_venue_adapter(venue_id)
        ask_every_question_answerable_without_venue_data(adapter)
        assert_data_questions_are_answered_not_inherited(adapter)


def test_the_question_set_names_every_abstract_method_of_the_shape():
    """The two tuples together are the whole shape, so nothing can be added unasked.

    Without this, a question added to VenueAdapter and forgotten in the tuples
    would be a venue oddity no conformance test ever puts to any adapter.
    """
    asked = set(QUESTIONS_ANSWERED_WITHOUT_VENUE_DATA) | set(QUESTIONS_ANSWERED_FROM_A_VENUE_MESSAGE)
    assert asked == set(VenueAdapter.__abstractmethods__)


class AdapterThatForgotOneQuestion(VenueAdapter):
    """Deliberately incomplete: proof the conformance check can actually fail.

    Rule 8 applied to a test rather than a tile -- a check that has never been
    seen to fail is not evidence, and a conformance suite that passes because it
    asked nothing is the exact failure this whole test module exists to prevent.
    """

    @property
    def venue_id(self) -> str:
        return "forgetful-venue"


def test_an_adapter_missing_a_question_cannot_be_built_at_all():
    with pytest.raises(TypeError) as refusal:
        AdapterThatForgotOneQuestion()
    assert "abstract" in str(refusal.value)


def test_a_venue_fact_without_a_source_is_refused():
    with pytest.raises(VenueFactWithoutSource):
        VenueFact(name="streams_per_connection", value=1024, unit="streams", source="   ")


def test_a_venue_id_maps_to_a_module_by_convention():
    assert module_name_for_venue("binance-usdm") == "binance_usdm"
    assert module_name_for_venue("okx_swap") == "okx_swap"


def test_a_venue_with_no_module_is_refused_by_name():
    with pytest.raises(VenueAdapterMissing) as refusal:
        load_venue_adapter("a-venue-nobody-wrote")
    assert "runtime.venues.a_venue_nobody_wrote" in str(refusal.value)


def test_a_module_without_the_factory_is_refused(monkeypatch):
    """A module that exists but is not the shape is named, not skipped."""
    import sys
    import types

    module = types.ModuleType("runtime.venues.shapeless_venue")
    monkeypatch.setitem(sys.modules, "runtime.venues.shapeless_venue", module)
    with pytest.raises(VenueAdapterMissing) as refusal:
        load_venue_adapter("shapeless-venue")
    assert ADAPTER_FACTORY_NAME in str(refusal.value)


def test_a_factory_returning_something_else_is_refused(monkeypatch):
    import sys
    import types

    module = types.ModuleType("runtime.venues.impostor_venue")
    setattr(module, ADAPTER_FACTORY_NAME, lambda: object())
    monkeypatch.setitem(sys.modules, "runtime.venues.impostor_venue", module)
    with pytest.raises(VenueAdapterMissing) as refusal:
        load_venue_adapter("impostor-venue")
    assert "not a VenueAdapter" in str(refusal.value)


def test_a_bare_string_of_venues_is_refused(tmp_path):
    """One venue written without brackets would be captured as its own letters."""
    path = tmp_path / "runtime.toml"
    path.write_text(
        '[captured_venues]\nvalue = "binance-usdm"\nunit = "venue ids"\nnote = "a typo"\n'
    )
    with pytest.raises(ValueError) as refusal:
        captured_venue_ids_from(path)
    assert CAPTURED_VENUES_SETTING in str(refusal.value)
