"""social-sentiment-reader: what a public forum sounded like, and who was talking.

Crypto social sentiment is bullish almost always. The level therefore carries very
little on its own -- a reading of "78% positive" is roughly the resting state, and a
system that treats it as a bullish signal is permanently long. What carries
information is the *change*, and even that only when the crowd is a crowd.

Two failures dominate this data source, and both are counted rather than filtered:

- **A campaign looks exactly like enthusiasm.** A hundred posts from six accounts
  and a hundred posts from ninety accounts read identically in any aggregate score.
  So posts and distinct accounts are both recorded, concentration is computed from
  them, and a concentrated reading is marked -- never quietly dropped, because a
  coordinated push is itself a real thing happening to the symbol.
- **The baseline drifts per symbol.** BTCUSDT and a three-day-old memecoin have
  completely different resting sentiment, so change is measured against each
  symbol's own history rather than against a shared neutral point.

What this part never does is convert sentiment into a direction. Sentiment is
recorded as a reading about a conversation; whether that conversation matters is a
judgement for a part that also knows the price, and mixing the two here would hide
the join behind one number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import (
    COMPLETE, PARTIAL, SentimentReading, SOCIAL, UNAVAILABLE,
)
from runtime.rolling_statistics import RollingWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "social-sentiment-reader"

PART_DECLARATION = PartDeclaration(
    part_id="social-sentiment-reader",
    consumes=("symbol-universe",),
    produces=("sentiment-reading", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
TOO_QUIET = "too-few-posts-to-be-a-crowd"
LOOKS_COORDINATED = "a-few-accounts-produced-most-of-the-posts"
NO_BASELINE = "this-symbol-has-no-history-to-measure-change-against"
READ_FAILED = "read-failed"


@dataclass(frozen=True)
class SentimentRead:
    symbol: str
    state: str
    reading: SentimentReading | None
    change: float | None
    concentration: float
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (READ, LOOKS_COORDINATED) and self.reading is not None


@dataclass
class SentimentStanding:
    reads_attempted: int = 0
    reads_succeeded: int = 0
    too_quiet: int = 0
    coordinated_readings: int = 0
    without_a_baseline: int = 0
    failures: int = 0
    symbols_with_a_baseline: int = 0


class SocialSentimentReader:
    """Reads a forum, records level, change against the symbol's own baseline, and who posted."""

    def __init__(
        self,
        minimum_posts: int,
        minimum_distinct_accounts: int,
        baseline_window: int,
        concentration_threshold: float,
        window_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_posts < 2:
            raise ValueError("one post is not a conversation")
        if minimum_distinct_accounts < 2:
            raise ValueError("one account is not a crowd")
        if baseline_window < 2:
            raise ValueError(
                "change is measured against this symbol's own history, which needs "
                "more than one past reading"
            )
        if not 0.0 < concentration_threshold < 1.0:
            raise ValueError("concentration is a fraction inside (0, 1)")
        self._minimum_posts = minimum_posts
        self._minimum_accounts = minimum_distinct_accounts
        self._baseline_window = baseline_window
        self._concentration_threshold = concentration_threshold
        self._window_seconds = window_seconds
        self._now_ns = now_ns
        self._baselines: dict[str, RollingWindow] = {}
        self._read_forum = None
        self.standing = SentimentStanding()

    def install_reader(self, read_forum) -> None:
        """`read_forum(symbol) -> (posts, source_reference, was_complete)`.

        A post is a mapping with an account and a score in [-1, 1]. Scoring text is
        somebody else's problem on purpose: this part is about who said it and how
        much of it there was, which is the half that gets skipped.
        """
        self._read_forum = read_forum

    def read(self, symbol: str) -> SentimentRead:
        self.standing.reads_attempted += 1
        if self._read_forum is None:
            raise RuntimeError("no forum reader is installed")

        try:
            posts, source_reference, was_complete = self._read_forum(symbol)
        except Exception as failure:
            self.standing.failures += 1
            return self._read(
                symbol, READ_FAILED, None, None, 0.0,
                f"the forum could not be read ({type(failure).__name__}). Silence and an "
                f"unread forum are different facts",
            )

        posts = tuple(posts or ())
        accounts = {post["account"] for post in posts}

        if len(posts) < self._minimum_posts or len(accounts) < self._minimum_accounts:
            self.standing.too_quiet += 1
            return self._read(
                symbol, TOO_QUIET, None, None, 0.0,
                f"{len(posts)} post(s) from {len(accounts)} account(s), below the "
                f"{self._minimum_posts}/{self._minimum_accounts} bar. Quiet is a reading "
                f"about the forum, not about the symbol",
            )

        level = sum(float(post["score"]) for post in posts) / len(posts)
        baseline = self._baselines.get(symbol)
        if baseline is None:
            baseline = RollingWindow(self._baseline_window)
            self._baselines[symbol] = baseline
            self.standing.symbols_with_a_baseline += 1

        # Change is against this symbol's own resting state. A shared neutral point
        # would call every liquid major permanently bullish.
        change = None
        if baseline.count >= self._baseline_window:
            change = level - baseline.mean(self._baseline_window)
        baseline.observe(level)

        reading = SentimentReading(
            symbol=symbol,
            level=level,
            change=change,
            posts=len(posts),
            distinct_accounts=len(accounts),
            window_seconds=self._window_seconds,
            completeness=COMPLETE if was_complete else PARTIAL,
            observed_at_ns=self._now_ns(),
            source_reference=source_reference,
        )

        if reading.looks_coordinated:
            self.standing.coordinated_readings += 1
            return self._read(
                symbol, LOOKS_COORDINATED, reading, change, reading.concentration,
                f"{len(posts)} post(s) from {len(accounts)} account(s), concentration "
                f"{reading.concentration:.0%}. This is kept rather than dropped -- a "
                f"coordinated push is a real thing happening to the symbol, it is just "
                f"not a crowd forming an opinion",
            )

        if change is None:
            self.standing.without_a_baseline += 1
            return self._read(
                symbol, NO_BASELINE, reading, None, reading.concentration,
                f"level {level:+.2f} recorded, but this symbol has "
                f"{baseline.count}/{self._baseline_window} readings of history. The level "
                f"alone says little: crypto forums are bullish almost always",
            )

        self.standing.reads_succeeded += 1
        return self._read(
            symbol, READ, reading, change, reading.concentration,
            f"level {level:+.2f}, {change:+.2f} against this symbol's own baseline, from "
            f"{len(posts)} post(s) across {len(accounts)} account(s)",
        )

    def _read(self, symbol, state, reading, change, concentration, reason) -> SentimentRead:
        return SentimentRead(
            symbol=symbol, state=state, reading=reading, change=change,
            concentration=concentration, reason=reason, read_at_ns=self._now_ns(),
        )


def describe_sentiment_reading(reader: SocialSentimentReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads_attempted": reader.standing.reads_attempted,
        "reads_succeeded": reader.standing.reads_succeeded,
        "too_quiet": reader.standing.too_quiet,
        "readings_that_look_coordinated": reader.standing.coordinated_readings,
        "readings_without_a_baseline": reader.standing.without_a_baseline,
        "failures": reader.standing.failures,
        "symbols_with_a_baseline": reader.standing.symbols_with_a_baseline,
        "converts_sentiment_into_a_direction": False,
        "uses_one_neutral_point_for_every_symbol": False,
    }


def run_social_sentiment_reader(
    reader: SocialSentimentReader, control_socket, read_universe, publish_readings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for symbol in read_universe():
            result = reader.read(symbol)
            if result.is_usable:
                publish_readings(result.reading)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sentiment_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    No forum reader is installed on this box, so every symbol in the
    universe is answered READ_FAILED by name and nothing is published;
    `install_reader` is the one way one gets in.
    """
    from runtime.input_assembly import Batch

    universe = Batch(read=context.bus.reader("symbol-universe"))
    publish_readings = context.bus.publisher_for("sentiment-reading")
    reader = SocialSentimentReader(
        minimum_posts=int(context.number("sentiment_minimum_posts")),
        minimum_distinct_accounts=int(context.number("sentiment_minimum_distinct_accounts")),
        baseline_window=int(context.number("sentiment_baseline_window")),
        concentration_threshold=context.number("sentiment_concentration_threshold"),
        window_seconds=context.number("sentiment_window_seconds"),
    )

    def read_universe():
        symbols: set[str] = set()
        for selection in universe.payloads():
            for entry in selection if isinstance(selection, (tuple, list)) else (selection,):
                symbols.add(entry.symbol)
        return tuple(sorted(symbols))

    return run_social_sentiment_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_universe=read_universe,
        publish_readings=lambda reading: publish_readings((reading,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
