"""news-source-health-monitor: state whether each news source is still delivering.

**A dead feed reads exactly like a quiet news day unless something measures it.**
That sentence is the news block's own design note and this part is the answer to
it: `news-source-standing` is what `alert-raiser` fires on, and `web-news-searcher`
reads it to decide whether it has to go looking itself.

## The bound is the source's own record, not a number somebody picked

Indian market news genuinely stops. NSE trades six and a quarter hours a day and
the Upstox news endpoint carried, on 2026-09-12, about two stories a day per
underlying — so a fixed silence bound short enough to catch a dead feed inside a
session fires every single night, and one long enough to survive the night cannot
notice a feed that died at the open.

So nothing here is picked. Each source teaches this part its own gaps:

    usual gap              the median of that source's observed gaps
    unusually quiet        past the 95th percentile of them
    not delivering         silent longer than that source has EVER been silent

The last line is the one that makes this honest. A source that has been quiet
for longer than its own record is, by its own evidence, not delivering — and the
record includes the overnight gap the moment the first night passes, so the
monitor calibrates itself against the market's real rhythm instead of being told
about it. No multiple of anything, no literal.

## A delivery is an occasion, not a row

This part measured rows for its first three minutes on the live spine, and two
sources immediately read `NOT_DELIVERING` on a perfectly healthy system. One
poll of NSE's F&O ban list carries a couple of hundred restriction reports:
counted as rows, the source appears to deliver twice a second, and then thirty
seconds of entirely ordinary quiet is longer than any gap it has ever shown.

So the rhythm is measured from **deliveries** — at most one per source per tick —
while the rows are still counted beside them (`reports_read` against
`deliveries_seen`). It is the same distinction `runtime/level_publishing.py`
draws on the write side: a level restated is not an event, and a poll that
carried two hundred names is one poll.

Until a source has enough gaps to state a median, its standing is
`NOT_MEASURED`. Rule 8: a source nobody has measured is not a healthy source,
and it is not a dead one either.

## Three wires, because a source is anything that feeds this block

`raw-news-item` names its own source. `instrument-restriction-report` carries a
`source` field — NSE's F&O ban list, an ASM stage list. `corporate-action-report`
carries none, because there is only ever one publisher of it, so the wire itself
is the source and is named here rather than invented per message. A source whose
reports stop is a source this block has gone blind on, and a restriction list
that quietly stopped updating is how a banned symbol gets traded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import QuantileEstimator
from runtime.news_types import Delivery, NewsSourceStanding
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "news-source-health-monitor"

PART_DECLARATION = PartDeclaration(
    part_id="news-source-health-monitor",
    consumes=(
        "raw-news-item",
        "corporate-action-report",
        "instrument-restriction-report",
    ),
    produces=("news-source-standing", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

NANOSECONDS_PER_SECOND = 1_000_000_000

# `CorporateActionReport` carries no `source` field, and correctly: NSE is the
# only publisher of one and every reader of the type treats it as NSE's own
# word. The wire is therefore the source, named once here rather than stamped
# onto every message by a part that would only be guessing it.
CORPORATE_ACTION_SOURCE_ID = "nse-corporate-actions"

USUAL_GAP_QUANTILE = 0.5
UNUSUALLY_QUIET_GAP_QUANTILE = 0.95


@dataclass
class SourceRecord:
    """One source's arrivals, and the gaps between them."""

    source_id: str
    items_seen: int = 0
    first_item_observed_at_ns: int | None = None
    last_item_observed_at_ns: int | None = None
    longest_gap_seconds: float = 0.0
    gaps: QuantileEstimator | None = None


@dataclass
class MonitorStanding:
    # Rows, across every wire. Kept beside `deliveries_seen` because the two
    # answer different questions: 244 rows in one poll is one delivery, and a
    # source's rhythm is the rhythm of its polls.
    reports_read: int = 0
    deliveries_seen: int = 0
    sources_known: int = 0
    sources_delivering: int = 0
    sources_quieter_than_usual: int = 0
    sources_not_delivering: int = 0
    sources_not_measured: int = 0
    standings_published: int = 0
    # An arrival stamped earlier than the one before it. Gaps are computed from
    # observation times, which this system stamps itself, so this should stay
    # zero -- if it does not, something is reordering messages and every gap
    # measured here is suspect.
    arrivals_out_of_order: int = 0


class NewsSourceHealthMonitor:
    """Whether each source that feeds this block is still delivering."""

    def __init__(
        self,
        window: int,
        minimum_gaps_to_state_a_habit: int,
        now_ns=time.time_ns,
    ) -> None:
        if window < 1:
            raise ValueError("the window must hold at least one gap")
        if minimum_gaps_to_state_a_habit < 1:
            raise ValueError(
                "minimum_gaps_to_state_a_habit must be at least one: it is how many "
                "gaps a source must show before this part will call it quiet, and zero "
                "would call every source quiet the moment it first spoke"
            )
        self._window = window
        self._minimum_gaps = minimum_gaps_to_state_a_habit
        self._now_ns = now_ns
        self._records: dict[str, SourceRecord] = {}
        self.standing = MonitorStanding()

    def observe_delivery(self, source_id: str, observed_at_ns: int) -> None:
        """One DELIVERY from one source, at the moment this system saw it.

        An occasion, not a row. One poll of NSE's F&O ban list carries a couple
        of hundred restriction reports and is one delivery; counting each row
        would tell this part that the source delivers twice a second, and then
        thirty seconds of entirely ordinary quiet is longer than any gap it has
        ever seen. That is not hypothetical -- it is what happened the first
        minutes this part ran on the live spine, and two sources read
        NOT_DELIVERING inside three minutes of a perfectly healthy system.
        `start_part` therefore calls this at most once per source per tick.
        """
        if not source_id:
            return
        self.standing.deliveries_seen += 1
        record = self._records.get(source_id)
        if record is None:
            record = SourceRecord(
                source_id=source_id,
                gaps=QuantileEstimator(window=self._window, prior=0.0),
            )
            self._records[source_id] = record
            self.standing.sources_known = len(self._records)
            record.first_item_observed_at_ns = observed_at_ns

        previous = record.last_item_observed_at_ns
        if previous is not None:
            gap_seconds = (observed_at_ns - previous) / NANOSECONDS_PER_SECOND
            if gap_seconds < 0:
                self.standing.arrivals_out_of_order += 1
            else:
                record.gaps.observe(gap_seconds)
                if gap_seconds > record.longest_gap_seconds:
                    record.longest_gap_seconds = gap_seconds
        record.items_seen += 1
        # Only advance on a forward arrival, so one out-of-order message cannot
        # drag this source's clock backwards and make it look freshly delivering.
        if previous is None or observed_at_ns > previous:
            record.last_item_observed_at_ns = observed_at_ns

    def standing_of(self, source_id: str, now_ns: int | None = None) -> NewsSourceStanding:
        """What is true about one source right now."""
        now = now_ns if now_ns is not None else self._now_ns()
        record = self._records[source_id]
        last = record.last_item_observed_at_ns
        silence_seconds = (
            (now - last) / NANOSECONDS_PER_SECOND if last is not None else None
        )
        usual = record.gaps.estimate(
            quantile=USUAL_GAP_QUANTILE, minimum_observations=self._minimum_gaps
        )
        unusually_quiet = record.gaps.estimate(
            quantile=UNUSUALLY_QUIET_GAP_QUANTILE,
            minimum_observations=self._minimum_gaps,
        )
        delivery = self._delivery_of(
            silence_seconds=silence_seconds,
            is_fitted=usual.is_fitted,
            unusually_quiet_past_seconds=unusually_quiet.value,
            longest_gap_seconds=record.longest_gap_seconds,
        )
        return NewsSourceStanding(
            source_id=source_id,
            delivery=delivery,
            items_seen=record.items_seen,
            last_item_observed_at_ns=last,
            seconds_since_last_item=silence_seconds,
            typical_seconds_between_items=usual.value if usual.is_fitted else None,
            observed_at_ns=now,
        )

    def _delivery_of(
        self,
        silence_seconds: float | None,
        is_fitted: bool,
        unusually_quiet_past_seconds: float,
        longest_gap_seconds: float,
    ) -> Delivery:
        """Which state this silence is, against this source's own record."""
        if silence_seconds is None or not is_fitted:
            # Not enough of this source's own history to say what quiet means
            # for it. Reported as its own state rather than as delivering: a
            # source nobody has measured is not a healthy source (Rule 8).
            return Delivery.NOT_MEASURED
        if silence_seconds > longest_gap_seconds:
            # Longer than this source has ever gone quiet before. Its own
            # record is the evidence, which is why no multiple and no literal
            # appears here -- and why the overnight gap folds itself in after
            # the first night rather than having to be told about.
            return Delivery.NOT_DELIVERING
        if silence_seconds > unusually_quiet_past_seconds:
            return Delivery.QUIETER_THAN_USUAL
        return Delivery.DELIVERING

    def every_standing(self, now_ns: int | None = None) -> tuple[NewsSourceStanding, ...]:
        """Every source's standing, and the counts that summarise them."""
        now = now_ns if now_ns is not None else self._now_ns()
        standings = tuple(
            self.standing_of(source_id, now) for source_id in sorted(self._records)
        )
        counted = {state: 0 for state in Delivery}
        for source_standing in standings:
            counted[source_standing.delivery] += 1
        self.standing.sources_delivering = counted[Delivery.DELIVERING]
        self.standing.sources_quieter_than_usual = counted[Delivery.QUIETER_THAN_USUAL]
        self.standing.sources_not_delivering = counted[Delivery.NOT_DELIVERING]
        self.standing.sources_not_measured = counted[Delivery.NOT_MEASURED]
        return standings

    @property
    def sources_known(self) -> int:
        return len(self._records)


def latest_delivery_per_source(rows) -> dict[str, int]:
    """One delivery per source out of a tick's rows, at its latest observation.

    This is the rule that keeps a poll from looking like a rhythm. One poll of
    NSE's F&O ban list is two hundred rows and one delivery; counted as rows the
    source appears to deliver twice a second, which is what made two sources
    read NOT_DELIVERING on a healthy live spine within three minutes.

    A row naming no source is dropped here rather than attributed: a delivery
    belongs to a source, and there is no honest source to give it to.
    """
    latest: dict[str, int] = {}
    for source_id, observed_at_ns in rows:
        if not source_id:
            continue
        if observed_at_ns > latest.get(source_id, 0):
            latest[source_id] = observed_at_ns
    return latest


def describe_monitoring(monitor: NewsSourceHealthMonitor) -> dict:
    standing = monitor.standing
    return {
        "part_id": PART_ID,
        "reports_read": standing.reports_read,
        "deliveries_seen": standing.deliveries_seen,
        "sources_known": monitor.sources_known,
        "sources_delivering": standing.sources_delivering,
        "sources_quieter_than_usual": standing.sources_quieter_than_usual,
        "sources_not_delivering": standing.sources_not_delivering,
        "sources_not_measured": standing.sources_not_measured,
        "standings_published": standing.standings_published,
        "arrivals_out_of_order": standing.arrivals_out_of_order,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisherByKey, without_observation_time

    monitor = NewsSourceHealthMonitor(
        window=int(context.number("news_source_gap_window_observations")),
        minimum_gaps_to_state_a_habit=int(
            context.number("news_source_minimum_gaps_to_state_a_habit")
        ),
    )
    items = Batch(read=context.bus.reader("raw-news-item"))
    corporate_actions = Batch(read=context.bus.reader("corporate-action-report"))
    restrictions = Batch(read=context.bus.reader("instrument-restriction-report"))

    # A standing per source, so one source going quiet does not restate every
    # other source's level. `without_observation_time` drops `observed_at_ns`
    # from the change check -- it is stamped every time this part looks, so
    # comparing it would republish an unchanged standing forever while the skip
    # counter claimed nothing was being skipped.
    publish_standings = LevelPublisherByKey(
        publish=context.bus.publisher_for("news-source-standing"),
        refresh_interval_seconds=context.number(
            "news_source_standing_refresh_interval_seconds"
        ),
        identity_of=without_observation_time,
    )

    def observe_all() -> None:
        """Every row that arrived this tick, as one delivery per source."""
        rows = tuple(_rows_of(items, corporate_actions, restrictions))
        monitor.standing.reports_read += len(rows)
        for source_id, observed_at_ns in latest_delivery_per_source(rows).items():
            monitor.observe_delivery(source_id, observed_at_ns)

    def _rows_of(items, corporate_actions, restrictions):
        for item in items.payloads():
            yield (
                str(getattr(item, "source_id", "") or ""),
                int(getattr(item, "observed_at_ns", 0) or time.time_ns()),
            )
        for report in corporate_actions.payloads():
            yield (
                CORPORATE_ACTION_SOURCE_ID,
                int(getattr(report, "observed_at_ns", 0) or time.time_ns()),
            )
        for report in restrictions.payloads():
            yield (
                str(getattr(report, "source", "") or ""),
                int(getattr(report, "observed_at_ns", 0) or time.time_ns()),
            )

    def tick() -> None:
        observe_all()
        # Every source every tick, because a source going quiet is a *change of
        # level* that no arriving message announces -- the whole point of this
        # part is that silence has to be looked for. The publisher only puts a
        # standing on the bus when that source's own level differs or its
        # refresh is due.
        for source_standing in monitor.every_standing():
            if publish_standings.publish_level(
                source_standing.source_id, (source_standing,)
            ):
                monitor.standing.standings_published += 1

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_monitoring(monitor),
    )


__all__ = [
    "CORPORATE_ACTION_SOURCE_ID",
    "MonitorStanding",
    "NewsSourceHealthMonitor",
    "PART_DECLARATION",
    "PART_ID",
    "SourceRecord",
    "UNUSUALLY_QUIET_GAP_QUANTILE",
    "USUAL_GAP_QUANTILE",
    "describe_monitoring",
    "latest_delivery_per_source",
    "start_part",
]
