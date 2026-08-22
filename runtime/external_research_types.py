"""The vocabulary the online-research block reads the outside world in.

Every part in that block takes in material this system did not produce and cannot
re-derive: a leaderboard's ranking, a wallet's balance change, a forum's mood, an
exchange's notice. T-4 forbids one part importing another, so the shapes they pass
between them live here, in one place, named for what they mean rather than for the
part that happens to write them.

Three properties are stamped on almost everything in this module, because outside
material without them is not usable:

- **where it came from**, precisely enough to go back to. A reading whose source
  cannot be named cannot be rechecked when it turns out to be wrong, and outside
  material turns out to be wrong regularly.
- **when it was true**, separately from when it was read. A leaderboard read at
  noon describes a week that ended at midnight, and treating the read time as the
  observation time makes stale material look fresh.
- **how complete it is.** A partial read is not a small read -- a top-10 slice of
  a leaderboard has a different meaning from the whole thing, and a flow figure
  covering three of eight exchanges is not a flow figure.

Nothing here is a claim about the market. These are records of what a source said,
which is a different kind of fact and is kept in a different shape on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How completely a source was read. A partial read is not a small read.
COMPLETE = "complete"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"

# What a source is, for the purposes of deciding how much it can carry.
SELF_REPORTED = "self-reported"          # the trader's own numbers
VENUE_PUBLISHED = "venue-published"      # the exchange's own leaderboard
ONCHAIN = "onchain"                      # settled on a chain, expensive to fake
SOCIAL = "social"                        # a forum, a chat, a post
DERIVED = "derived"                      # this system computed it from the above

SOURCE_KINDS = (SELF_REPORTED, VENUE_PUBLISHED, ONCHAIN, SOCIAL, DERIVED)


@dataclass(frozen=True)
class TrackedTrader:
    """Somebody whose positions are worth reading, and why they came to notice.

    `identity_reference` is whatever the venue or chain uses -- a leaderboard
    handle, an address, an encrypted UID. It is deliberately opaque: nothing in
    this system needs to know who a person is, only whether their positions have
    predicted anything.
    """

    trader_id: str
    venue_id: str
    identity_reference: str
    source_kind: str
    rank: int | None
    period_days: int | None
    reported_return: float | None
    is_public_by_choice: bool
    first_seen_at_ns: int
    observed_at_ns: int

    @property
    def can_be_read_further(self) -> bool:
        """A trader who hid their positions is tracked but not readable."""
        return self.is_public_by_choice


@dataclass(frozen=True)
class ExternalPosition:
    """A position somebody else holds, as far as a public source shows it.

    `is_full_book` is the field that decides what may be concluded. One visible
    long says nothing on its own -- it may be a hedge against something invisible,
    and copying half of a pair is worse than copying neither leg.
    """

    trader_id: str
    venue_id: str
    symbol: str
    side: str
    notional: float | None
    entry_price: float | None
    leverage: float | None
    opened_at_ns: int | None
    observed_at_ns: int
    is_full_book: bool
    completeness: str
    source_reference: str

    @property
    def can_be_reasoned_about_alone(self) -> bool:
        return self.is_full_book


@dataclass(frozen=True)
class VerifiedRecord:
    """What of a trader's claimed record actually checks out.

    A record is three separable things, and conflating them is how a survivor of
    one lucky quarter reads as a professional: how long it is, how much of it can
    be seen rather than taken on trust, and how it was made. A 400% return from
    one 50x position is a different object from a 40% return from 900 trades.
    """

    trader_id: str
    claimed_return: float | None
    verifiable_return: float | None
    trades_seen: int
    days_covered: float
    largest_single_contribution: float | None
    was_leveraged_beyond: float | None
    verification_state: str
    reason: str
    verified_at_ns: int

    @property
    def is_one_lucky_trade(self) -> bool:
        """The single most common shape behind a spectacular public record."""
        return (
            self.largest_single_contribution is not None
            and self.largest_single_contribution > 0.5
        )


@dataclass(frozen=True)
class CopyLatency:
    """How far the price moved between somebody acting and this system seeing it.

    The number that decides whether copying is a strategy or a subsidy. It is
    measured, never assumed, and it is measured per venue and per symbol because
    a thin altcoin moves further in the same seconds than BTCUSDT does.
    """

    venue_id: str
    symbol: str
    detection_delay_seconds: float
    adverse_move_fraction: float
    observations: int
    is_fitted: bool
    measured_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.is_fitted and self.observations > 0


@dataclass(frozen=True)
class CopyScore:
    """Whether following this trader would have been worth it after the delay.

    Scored on what copying would have returned, not on what the trader returned.
    Those differ by exactly the latency, and the gap is the whole question.
    """

    trader_id: str
    symbol: str | None
    score: float
    their_return: float | None
    copyable_return: float | None
    lost_to_latency: float | None
    state: str
    reason: str
    scored_at_ns: int

    @property
    def is_worth_copying(self) -> bool:
        return self.state == "worth-copying"


@dataclass(frozen=True)
class ResearchFinding:
    """Something learned from outside, in a form that can be argued with.

    A finding is not a fact. It carries what it was derived from, how strongly,
    and what would change it -- because most outside material is somebody's
    conclusion, and adopting a conclusion without its evidence is how a system
    ends up confidently holding a stranger's mistake.
    """

    finding_id: str
    topic: str
    statement: str
    evidence: tuple
    source_references: tuple
    confidence: float
    would_be_refuted_by: str
    is_testable_here: bool
    found_at_ns: int

    @property
    def can_be_acted_on(self) -> bool:
        """Only a testable finding may reach a decision; the rest is reading."""
        return self.is_testable_here and bool(self.source_references)


@dataclass(frozen=True)
class StrategyGap:
    """Something others appear to be doing that this system is not.

    A gap is only interesting if it is reachable. "They trade a venue we have no
    account on" and "they take a setup our scanner never looks for" are both gaps,
    and only the second is worth a hypothesis.
    """

    gap_id: str
    description: str
    their_edge: float | None
    our_edge: float | None
    difference: float | None
    is_reachable_here: bool
    blocked_by: str | None
    evidence_count: int
    found_at_ns: int

    @property
    def is_worth_a_hypothesis(self) -> bool:
        return self.is_reachable_here and self.evidence_count > 1


@dataclass(frozen=True)
class WhaleTransfer:
    """A large on-chain movement, and the only thing it reliably means.

    Direction matters and destination matters more: coins moving to an exchange
    deposit address can be sold, coins moving out cannot be sold there. Almost
    everything else said about whale transfers is narration.
    """

    transfer_id: str
    chain: str
    asset: str
    quantity: float
    quote_value: float | None
    from_kind: str
    to_kind: str
    to_venue_id: str | None
    confirmed_at_ns: int
    observed_at_ns: int
    source_reference: str

    @property
    def could_become_supply(self) -> bool:
        return self.to_kind == "exchange-deposit"

    @property
    def leaves_the_market(self) -> bool:
        return self.from_kind == "exchange-withdrawal" or self.to_kind == "custody"


@dataclass(frozen=True)
class OnchainFlow:
    """Net movement into or out of exchanges over a window, and its coverage.

    Coverage is not a footnote. A net-flow figure covering three of eight known
    exchange clusters is not a small version of the real figure -- it can have the
    opposite sign.
    """

    asset: str
    window_seconds: float
    inflow: float
    outflow: float
    clusters_covered: int
    clusters_known: int
    completeness: str
    measured_at_ns: int

    @property
    def net_flow(self) -> float:
        return self.inflow - self.outflow

    @property
    def coverage_fraction(self) -> float:
        if self.clusters_known <= 0:
            return 0.0
        return self.clusters_covered / self.clusters_known


@dataclass(frozen=True)
class SentimentReading:
    """What a public forum sounded like, with the two numbers that matter.

    The level is nearly useless on its own -- crypto social sentiment is bullish
    almost always -- so what is carried is the level *and* the change, plus how
    concentrated the posting was. A hundred posts from six accounts is a campaign
    wearing the shape of a crowd.
    """

    symbol: str
    level: float
    change: float | None
    posts: int
    distinct_accounts: int
    window_seconds: float
    completeness: str
    observed_at_ns: int
    source_reference: str

    @property
    def concentration(self) -> float:
        """1.0 means every post came from one account."""
        if self.posts <= 0:
            return 0.0
        return 1.0 - (self.distinct_accounts - 1) / max(self.posts - 1, 1)

    @property
    def looks_coordinated(self) -> bool:
        return self.posts >= 10 and self.concentration > 0.7


@dataclass(frozen=True)
class VenueAnnouncement:
    """Something the exchange itself said, and whether it changes the rules.

    Announcements are the one outside source that is not an opinion: a delisting
    notice, a leverage-tier change or a funding-interval change is the venue
    telling this system that its own model of the venue is now wrong.
    """

    announcement_id: str
    venue_id: str
    symbols: tuple
    kind: str
    headline: str
    effective_at_ns: int | None
    published_at_ns: int
    observed_at_ns: int
    source_reference: str

    @property
    def changes_how_a_symbol_trades(self) -> bool:
        return self.kind in (
            "delisting", "leverage-change", "funding-change",
            "settlement-change", "trading-halt", "tick-size-change",
        )


@dataclass(frozen=True)
class OptionsFlow:
    """Large options activity, kept as positioning rather than as a signal.

    The useful content is where risk was placed and at what expiry, not a
    call/put ratio. A ratio collapses strike and tenor into one number and then
    gets read as directional conviction, which it is not.
    """

    symbol: str
    underlying: str
    expiry_ns: int
    strike: float
    option_kind: str
    side: str
    contracts: float
    premium: float | None
    implied_volatility: float | None
    is_block: bool
    observed_at_ns: int
    source_reference: str

    @property
    def days_to_expiry(self) -> float:
        return max(self.expiry_ns - self.observed_at_ns, 0) / 86_400e9


@dataclass(frozen=True)
class WebIdea:
    """A raw idea found in public code or writing, before anything is believed.

    Kept deliberately weak: an idea is a candidate for a hypothesis, never a
    finding. The block that reads it is not allowed to conclude, only to notice.
    """

    idea_id: str
    summary: str
    origin_reference: str
    origin_kind: str
    stars_or_reach: int | None
    mentions_a_market: bool
    observed_at_ns: int


def unreadable(reason: str) -> str:
    """One phrasing for the same recurring fact, so it reads the same everywhere."""
    return f"nothing was read: {reason}"
