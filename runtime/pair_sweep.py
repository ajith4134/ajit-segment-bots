"""Every pair of a set, a budget at a time, resuming where the last pass stopped.

Two parts measure something about every pair of the symbols they know --
`correlation-cluster-mapper` correlates returns, `cointegration-pair-finder`
tests whether a spread reverts -- and pairs grow with the square of the universe
while the answers do not. At the 2,444-share cash-equity universe that is
2,985,346 pairs.

Neither part may build that list. Measured on this box 2026-09-05: materialising
it costs 252 MB and a quarter of a second, per pass, to look at a few thousand
of them -- the shape of defect this project keeps finding, where the bookkeeping
of a fix costs more than the work it saved. So this yields pairs lazily, from a
cursor, and never holds more than one at a time.

**It wraps exactly once.** A pass resuming near the end of the sweep still gets
its whole budget, and no pair is yielded twice within one pass -- which would
spend budget on a pair already measured and leave another unreached forever.

**A cursor whose symbols are gone restarts the sweep** rather than guessing a
position in a list that changed underneath it. Symbols come and go as the feed
does, and an index into yesterday's list points at a different pair today.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence


def pairs_after(
    groups: Mapping[str, Sequence[str]],
    resume_after: tuple[str, str, str] | None = None,
) -> Iterator[tuple[str, str, str]]:
    """Yield `(group, left, right)` for every pair within each group.

    Groups are walked in the order the mapping gives them and pairs within a
    group in the order its sequence gives them, so a caller that hands over
    sorted input gets a stable sweep -- which is what makes a cursor meaningful.
    A pair is only ever formed within one group: two symbols on different venues
    are not a pair, because the prices did not arrive from the same book.
    """
    ordered = [(group, list(symbols)) for group, symbols in groups.items()]
    start = _position_of(ordered, resume_after)
    yield from _walk(ordered, start, None)
    if start is not None:
        yield from _walk(ordered, None, start)


def _position_of(ordered, resume_after):
    """Where the sweep resumes: just past the cursor, or None to start at the top."""
    if resume_after is None:
        return None
    group, left, right = resume_after
    for group_index, (name, symbols) in enumerate(ordered):
        if name != group:
            continue
        try:
            left_index = symbols.index(left)
            right_index = symbols.index(right)
        except ValueError:
            return None
        if right_index + 1 < len(symbols):
            return (group_index, left_index, right_index + 1)
        if left_index + 2 < len(symbols):
            return (group_index, left_index + 1, left_index + 2)
        return (group_index + 1, 0, 1)
    return None


def _walk(ordered, start, stop):
    """From `start` (or the top) until `stop` (or the end), never past it."""
    group_index = 0 if start is None else start[0]
    while group_index < len(ordered):
        name, symbols = ordered[group_index]
        left = start[1] if start is not None and group_index == start[0] else 0
        while left < len(symbols) - 1:
            right = (
                start[2]
                if start is not None and group_index == start[0] and left == start[1]
                else left + 1
            )
            while right < len(symbols):
                if stop is not None and (group_index, left, right) == stop:
                    return
                yield name, symbols[left], symbols[right]
                right += 1
            left += 1
        group_index += 1


__all__ = ["pairs_after"]
