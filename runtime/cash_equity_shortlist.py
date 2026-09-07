"""Ranks cash-equity candidates into the day's tradeable shortlist.

The operator asked (2026-09-05) for the top 50 cash-equity names actually worth
scanning today, picked from live momentum, volume, 52-week range, gap, VWAP
deviation and volatility-normalised move -- not a fixed list, and not open
interest, which does not exist on a cash equity (that is a derivatives fact).

Extracted as a pure function, the same shape the retired crypto reader's
`select_capturable_symbols`/`_rank_pool_by_blended_percentiles` used
(`parts/market_data_feed/symbol_catalogue_reader.py`, off the spine since
2026-09-02): a pure function is testable without a running part and reusable by
both the live ranker part and `operate/replay_a_captured_session.py` -- a replay
that ranked candidates by a second, drifted copy of this logic could disagree
with what the live spine would have chosen, which is exactly the classification
mismatch `segment_that_trades` exists to prevent.

Every signal is a percentile rank within the liquidity-qualified pool, never a
raw magnitude blended directly: average daily volume runs in the tens of
millions, an ATR-normalised move in low single digits -- adding them
un-normalised would let volume decide the order by itself.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CashEquityCandidate:
    """One symbol's signals for one ranking pass.

    A signal this reader could not measure is `None`, and a `None` signal ranks
    as the pool's worst on that signal rather than being dropped: an unmeasured
    stock is not a flat one, and dropping it would let a data gap silently
    remove a real candidate from consideration.
    """

    symbol: str
    last_price: float | None = None
    session_open: float | None = None
    previous_close: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    average_daily_volume: float | None = None
    volume_so_far: float | None = None
    average_true_range: float | None = None
    session_vwap: float | None = None
    liquidity_spread_fraction: float | None = None


class ShortlistWeightsInvalid(ValueError):
    """The six blended weights do not sum to one.

    Refused rather than normalised: silently rescaling would let a settings typo
    quietly change what "50% momentum" means without anyone stating a new
    number (RL-061).
    """


@dataclass(frozen=True)
class ShortlistWeights:
    """How much each opportunity signal counts in the blend, once the liquidity
    floor has already chosen the eligible pool. Liquidity itself is not a
    blended weight here -- it is the pool cut `liquidity_pool_size` performs,
    the same role volume's floor played in the retired crypto blend -- so these
    six are the whole of the blend and must sum to 1.0.
    """

    momentum_weight: float
    volume_weight: float
    week_52_weight: float
    gap_weight: float
    vwap_weight: float
    atr_weight: float

    def __post_init__(self) -> None:
        total = (
            self.momentum_weight + self.volume_weight + self.week_52_weight
            + self.gap_weight + self.vwap_weight + self.atr_weight
        )
        if abs(total - 1.0) > 1e-9:
            raise ShortlistWeightsInvalid(
                f"the six shortlist weights sum to {total!r}, not 1.0 -- a blend whose "
                f"shares do not add to the whole is not a percentage of anything"
            )


def _momentum(entry: CashEquityCandidate) -> float | None:
    """Today's move so far, as a fraction of the session's own open."""
    if entry.last_price is None or not entry.session_open:
        return None
    return abs(entry.last_price - entry.session_open) / entry.session_open


def _gap(entry: CashEquityCandidate) -> float | None:
    """The session's open against the prior close, as a fraction of that close."""
    if entry.session_open is None or not entry.previous_close:
        return None
    return abs(entry.session_open - entry.previous_close) / entry.previous_close


def _vwap_deviation(entry: CashEquityCandidate) -> float | None:
    """How far the last price sits from the session's own volume-weighted average."""
    if entry.last_price is None or not entry.session_vwap:
        return None
    return abs(entry.last_price - entry.session_vwap) / entry.session_vwap


def _week_52_proximity(entry: CashEquityCandidate) -> float | None:
    """How close the last price sits to an edge of its own 52-week range, as a
    fraction of the range's own width: 0 at the midpoint, 0.5 at either edge.
    Larger is closer to a new high or low -- the breakout/breakdown reading the
    operator asked for, not merely "the range is wide"."""
    if (
        entry.last_price is None or entry.week_52_high is None or entry.week_52_low is None
        or entry.week_52_high <= entry.week_52_low
    ):
        return None
    width = entry.week_52_high - entry.week_52_low
    midpoint = (entry.week_52_high + entry.week_52_low) / 2.0
    return abs(entry.last_price - midpoint) / width


def _volume_surge(entry: CashEquityCandidate) -> float | None:
    """Today's traded volume so far, against this name's own average day."""
    if entry.volume_so_far is None or not entry.average_daily_volume:
        return None
    return entry.volume_so_far / entry.average_daily_volume


def _atr_normalised_move(entry: CashEquityCandidate) -> float | None:
    """Today's move sized against the name's own average true range, so a ₹5
    move on a ₹200 stock and a ₹50 move on a ₹2,000 stock are compared on the
    same footing instead of on raw percent."""
    if entry.last_price is None or entry.session_open is None or not entry.average_true_range:
        return None
    return abs(entry.last_price - entry.session_open) / entry.average_true_range


# Every signal in the blend, paired with the weight that names its share.
# Ordered here once so the ranking loop and any caller inspecting the blend
# read the same list.
_SIGNALS = (
    ("momentum", _momentum, "momentum_weight"),
    ("volume_surge", _volume_surge, "volume_weight"),
    ("week_52_proximity", _week_52_proximity, "week_52_weight"),
    ("gap", _gap, "gap_weight"),
    ("vwap_deviation", _vwap_deviation, "vwap_weight"),
    ("atr_normalised_move", _atr_normalised_move, "atr_weight"),
)


def rank_cash_equity_candidates(
    candidates: list[CashEquityCandidate],
    shortlist_size: int,
    weights: ShortlistWeights,
    liquidity_pool_size: int,
) -> tuple[str, ...]:
    """The day's top `shortlist_size` symbols, nearest the money on every signal.

    The liquidity floor is not negotiable, the same reasoning the retired
    crypto reader's volume floor enforced: only the top `liquidity_pool_size` by
    tightest spread are eligible at all, so a thin, hard-to-fill name cannot
    outrank a genuinely liquid one purely by having moved the most. A symbol
    with no liquidity reading ranks last on that cut rather than being dropped
    outright -- an unmeasured spread is not an infinite one, but it is not a
    zero one either.

    **Every ordering among unmeasured names is by average daily volume, and
    that is what stops this function sealing itself shut (2026-09-07).** A spread
    reading exists only for a symbol the feed is subscribed to, the feed
    subscribes what this function shortlists, and this function used
    `entry.symbol` as the tie-break wherever a reading was missing -- in the pool
    cut and, more damagingly, inside `percentile_rank`, where a missing reading
    does not produce a tie but a *position*, so the alphabet became the score. The
    first ranking of a day has no spreads and no prices at all, so all six signals
    were None for every candidate and the blend was six copies of the alphabet. It
    returned the head of the NSE master, the feed subscribed those, they became
    the only names ever measured, and every ranking after it returned the same
    list. Measured on the live spine on 2026-09-07, the cash-equity segment was
    subscribed to `3PLAND`, `63MOONS`, `AADHARHFC`, `AARTIIND`, `ABGSEC`,
    `ABSLLIQUID` (a liquid-fund ETF), `ABSLBANETF` and thirty-two more of the
    same shape, out of 2,654 candidates. They print 20 to 700 times a session
    against MARUTI's 8,002, never fill `mean-reversion-detector`'s 256-deep
    window, and so raised not one candidate: the segment formed **zero** trade
    intents all session and `instrument-selector` recorded zero refusals for it,
    because nothing ever arrived to refuse.

    Average daily volume is the right escape because it is the one liquidity fact
    that does **not** require a subscription: `equity-opportunity-profiler`
    fetches it from history for every ordinary share, subscribed or not. So an
    unmeasured name is ordered by how much it really trades, not by its initial,
    and the names that enter the shortlist on that basis are priced from the next
    cycle onward and then compete on the live blend like everything else.
    """
    if shortlist_size < 1:
        raise ValueError(
            f"shortlist_size is {shortlist_size!r}; a shortlist of fewer than one "
            f"symbol trades nothing"
        )
    if liquidity_pool_size < shortlist_size:
        raise ValueError(
            f"liquidity_pool_size ({liquidity_pool_size}) is smaller than shortlist_size "
            f"({shortlist_size}); the pool must hold at least as many qualified names as "
            f"the shortlist asks for"
        )

    by_liquidity = sorted(
        candidates,
        key=lambda entry: (
            entry.liquidity_spread_fraction is None,
            entry.liquidity_spread_fraction if entry.liquidity_spread_fraction is not None else 0.0,
            # Among names with no spread reading, the most-traded first. Their
            # spreads are unknown precisely because nothing is subscribed to
            # them, and volume is the liquidity fact that survives that.
            -(entry.average_daily_volume or 0.0),
            entry.symbol,
        ),
    )
    pool = by_liquidity[:liquidity_pool_size]
    if len(pool) <= 1:
        return tuple(entry.symbol for entry in pool[:shortlist_size])

    def percentile_rank(signal) -> dict[str, float]:
        # Best (largest opportunity) first; a signal this candidate has no
        # reading for sorts last -- worst on that one signal, not dropped.
        #
        # **Among candidates with no reading, by average daily volume.** This is
        # the tie-break that closed the loop: a percentile is a *position*, so
        # ordering the unmeasured by `entry.symbol` did not leave them tied, it
        # gave them distinct scores running from best to worst in alphabetical
        # order. On the first ranking of a day nothing has a live price, every one
        # of the six signals is None for every candidate, and the blend was
        # therefore six copies of the alphabet -- which is how the cash-equity
        # segment came to be subscribed to 3PLAND, 63MOONS, ABGSEC and ABSLLIQUID
        # out of 2,654 shares, and then stayed there, because those were the only
        # names anything ever measured.
        ordered = sorted(
            pool,
            key=lambda entry: (
                signal(entry) is None,
                -(signal(entry) or 0.0),
                -(entry.average_daily_volume or 0.0),
                entry.symbol,
            ),
        )
        return {entry.symbol: i / (len(pool) - 1) for i, entry in enumerate(ordered)}

    percentiles = [
        (percentile_rank(signal), getattr(weights, weight_name))
        for _name, signal, weight_name in _SIGNALS
    ]

    def blended_score(entry: CashEquityCandidate) -> float:
        return sum(pct[entry.symbol] * weight for pct, weight in percentiles)

    # A genuine tie -- two names alike on all six signals and on volume -- is
    # broken by symbol, which is the only deterministic answer left.
    ranked = sorted(
        pool,
        key=lambda entry: (
            blended_score(entry), -(entry.average_daily_volume or 0.0), entry.symbol,
        ),
    )
    return tuple(entry.symbol for entry in ranked[:shortlist_size])


__all__ = [
    "CashEquityCandidate",
    "ShortlistWeights",
    "ShortlistWeightsInvalid",
    "rank_cash_equity_candidates",
]
