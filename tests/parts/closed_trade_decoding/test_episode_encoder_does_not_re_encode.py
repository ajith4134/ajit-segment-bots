"""One closed trade is one episode, not one episode per tick forever.

`LatestByKey.mapping()` returns every key it still retains rather than the ones
that just arrived, so the encoder's `touched` set re-collected every trade it had
ever seen on every tick and re-encoded all of them. Found live on 2026-08-25: a
single closed trade had become 3,048 episodes and was still climbing, with 3,047
supersessions, and `winner-pattern-miner` had counted that one trade 2,825,907
times.

Two costs, and the second is the one that matters. The waste grows without bound
as trades accumulate. And every downstream part that does not de-duplicate is
counting the same trade thousands of times, which corrupts exactly the statistics
this block exists to produce -- a pattern miner told the same trade three thousand
times will call it a pattern.

`sequence-pattern-miner` was reporting `trades_recorded: 1` for the same trade
throughout, which is what made the encoder identifiable as the one at fault.

The rule this pins: **re-encode when an analysis actually lands, which is what
supersession is for -- not when the same analyses are merely still retained.**
"""

from __future__ import annotations

from dataclasses import dataclass

from parts.closed_trade_decoding.trade_episode_encoder import (
    TradeEpisodeEncoder,
    describe_episode_encoding,
)


@dataclass(frozen=True)
class Retained:
    """Stands in for LatestByKey: it keeps returning what it holds, every tick."""

    trade_id: str
    value: float = 1.0


def read_trades_like_the_encoder_does(retained, closed_by_id, encoded_from, standing):
    """The de-duplication `start_part` performs, exercised without a live bus.

    Mirrors the real loop: collect every trade any retained analysis still names,
    then skip the ones whose analyses have not moved since they were encoded.
    """
    touched = set()
    for source in retained.values():
        for trade_id in source:
            if trade_id in closed_by_id:
                touched.add(trade_id)

    jobs = []
    for trade_id in sorted(touched):
        analyses = tuple(source.get(trade_id) for source in retained.values())
        if encoded_from.get(trade_id) == analyses:
            standing.re_encodes_skipped += 1
            continue
        encoded_from[trade_id] = analyses
        jobs.append(trade_id)
    return jobs


def test_a_trade_whose_analyses_have_not_moved_is_not_re_encoded():
    """Ten ticks, one closed trade, nothing new: one encode and nine skips."""
    encoder = TradeEpisodeEncoder()
    retained = {"pnl-attribution": {"t1": Retained("t1")}, "entry-quality": {"t1": Retained("t1")}}
    closed_by_id = {"t1": object()}
    encoded_from: dict[str, tuple] = {}

    encoded = []
    for _ in range(10):
        encoded.extend(
            read_trades_like_the_encoder_does(
                retained, closed_by_id, encoded_from, encoder.standing
            )
        )

    assert encoded == ["t1"]
    assert encoder.standing.re_encodes_skipped == 9


def test_a_new_analysis_landing_does_re_encode():
    """Supersession still works -- that is the behaviour being preserved."""
    encoder = TradeEpisodeEncoder()
    retained = {"pnl-attribution": {"t1": Retained("t1", 1.0)}}
    closed_by_id = {"t1": object()}
    encoded_from: dict[str, tuple] = {}

    first = read_trades_like_the_encoder_does(retained, closed_by_id, encoded_from, encoder.standing)
    again = read_trades_like_the_encoder_does(retained, closed_by_id, encoded_from, encoder.standing)
    # A genuinely new attribution for the same trade.
    retained["pnl-attribution"]["t1"] = Retained("t1", 2.0)
    after = read_trades_like_the_encoder_does(retained, closed_by_id, encoded_from, encoder.standing)

    assert first == ["t1"]
    assert again == []
    assert after == ["t1"], "a new analysis must supersede the earlier episode"


def test_each_trade_is_tracked_separately():
    """One trade going quiet must not suppress another that just changed."""
    encoder = TradeEpisodeEncoder()
    retained = {"pnl-attribution": {"t1": Retained("t1"), "t2": Retained("t2")}}
    closed_by_id = {"t1": object(), "t2": object()}
    encoded_from: dict[str, tuple] = {}

    assert read_trades_like_the_encoder_does(
        retained, closed_by_id, encoded_from, encoder.standing
    ) == ["t1", "t2"]

    retained["pnl-attribution"]["t2"] = Retained("t2", 9.0)
    assert read_trades_like_the_encoder_does(
        retained, closed_by_id, encoded_from, encoder.standing
    ) == ["t2"]


def test_the_skip_count_is_on_the_standing_so_the_board_can_see_it():
    """A large number here is worth seeing; a zero once trades close is a broken check."""
    encoder = TradeEpisodeEncoder()
    encoder.standing.re_encodes_skipped = 7
    assert describe_episode_encoding(encoder)["re_encodes_skipped"] == 7


def test_the_growth_this_prevents_is_bounded_not_merely_smaller():
    """The defect grew without bound; the fix must be flat, not just slower."""
    encoder = TradeEpisodeEncoder()
    retained = {"pnl-attribution": {"t1": Retained("t1")}}
    closed_by_id = {"t1": object()}
    encoded_from: dict[str, tuple] = {}

    total = 0
    for _ in range(500):
        total += len(
            read_trades_like_the_encoder_does(
                retained, closed_by_id, encoded_from, encoder.standing
            )
        )

    assert total == 1, f"500 ticks produced {total} encodes of one unchanged trade"
