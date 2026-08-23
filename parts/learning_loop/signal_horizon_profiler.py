"""signal-horizon-profiler: how long each detector's claims take the market to settle.

The other half of what an exit plan cannot be built without, and it exists for the
same reason `signal-excursion-profiler` does: the only measurement of how long a
trade takes was a closed trade, and a trade cannot be opened without a plan
(`docs/proposals/live-excursion-and-horizon-profiling.md`).

**Per detector, not per symbol.** How long a move takes is a property of what was
noticed rather than of the book it was noticed in: a liquidation cascade and a
cointegration spread resolve on different clocks in the same symbol. The proposer
looks the horizon up by detector for exactly this reason.

**The median, not the mean.** A claim that resolved in two seconds and one that ran
the full horizon are both ordinary, and the mean of that distribution is dragged by
whichever tail the sample happened to catch. The median is the time half of this
detector's claims were settled by, which is the number a horizon should be.

**Unresolved claims are not counted.** `signal-outcome-labeller` drops a claim
whose horizon expired with neither barrier reached, so this part never sees one --
which is correct: "the move did not happen" is not evidence about how long the move
takes. What it costs is stated rather than hidden: this median describes claims
that resolved, so it is a measure of how quickly this detector is proved right or
wrong, not of how long a position must be held to find out.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_profiles import FROM_SETTLED_CLAIMS, HorizonProfile, quantile_of

PART_ID = "signal-horizon-profiler"

PART_DECLARATION = PartDeclaration(
    part_id="signal-horizon-profiler",
    consumes=("training-label",),
    produces=("horizon-profile", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MEASURED = "measured"
TOO_FEW_CLAIMS = "too-few-settled-claims-to-take-a-median"
REFUSED_NO_DETECTOR = "the-label-names-no-detector"
REFUSED_NO_SETUP_VERDICT = "the-label-says-nothing-about-whether-the-setup-was-right"
REFUSED_NOT_A_DURATION = "the-label-reports-no-time-to-resolve"

# The median. Named rather than written as 0.5 at the call site, because a
# quantile is a decision about what the number means and a bare literal in the
# middle of a call is a decision nobody can find later (RL-061).
MEDIAN = 0.5


@dataclass
class HorizonStanding:
    labels_seen: int = 0
    claims_recorded: int = 0
    profiles_published: int = 0
    profiles_fitted: int = 0
    refused: int = 0
    detectors_tracked: int = 0
    slowest_median_seconds: float = 0.0
    by_refusal: dict = field(default_factory=dict)


class SignalHorizonProfiler:
    """Keeps how long each detector's settled claims took, and reports the median."""

    def __init__(self, window: int, minimum_claims: int, now_ns=time.time_ns) -> None:
        if window < 2:
            raise ValueError("a median over fewer than two observations is one observation")
        if minimum_claims < 2:
            raise ValueError(
                "a median taken over one claim is that claim, reported with the authority "
                "of a distribution"
            )
        self._window = window
        self._minimum_claims = minimum_claims
        self._now_ns = now_ns
        self._durations: dict[str, deque] = {}
        self.standing = HorizonStanding()

    def observe_label(self, label) -> str:
        """Record how long one settled claim took, or refuse it and say why."""
        self.standing.labels_seen += 1

        if not label.detector:
            return self._refuse(REFUSED_NO_DETECTOR)
        if label.label_for(THE_SETUP_WAS_RIGHT) is None:
            return self._refuse(REFUSED_NO_SETUP_VERDICT)
        if label.seconds_to_resolve is None or label.seconds_to_resolve <= 0:
            # A claim that resolved in no time did not resolve; it was opened at a
            # price that had already crossed a barrier, and counting it would pull
            # every horizon towards zero.
            return self._refuse(REFUSED_NOT_A_DURATION)

        self._durations.setdefault(label.detector, deque(maxlen=self._window)).append(
            float(label.seconds_to_resolve)
        )
        self.standing.claims_recorded += 1
        self.standing.detectors_tracked = len(self._durations)
        return MEASURED

    def profile(self, detector: str) -> HorizonProfile:
        """One detector's horizon, fitted or honestly not."""
        durations = self._durations.get(detector, ())
        observed = len(durations)
        now = self._now_ns()

        if observed < self._minimum_claims:
            return HorizonProfile(
                detector=detector,
                median_seconds=0.0,
                trades_observed=observed,
                is_fitted=False,
                source=FROM_SETTLED_CLAIMS,
                reason=(
                    f"{observed} settled claim(s) of the {self._minimum_claims} this needs; "
                    f"until then no plan may name a horizon from it"
                ),
                measured_at_ns=now,
            )

        median = quantile_of(durations, MEDIAN)
        self.standing.profiles_fitted += 1
        self.standing.slowest_median_seconds = max(self.standing.slowest_median_seconds, median)
        return HorizonProfile(
            detector=detector,
            median_seconds=median,
            trades_observed=observed,
            is_fitted=True,
            source=FROM_SETTLED_CLAIMS,
            reason=(
                f"half of {detector}'s last {observed} settled claim(s) were resolved within "
                f"{median:.0f}s; measured from claims the market settled, not from closed "
                f"trades, which do not exist yet"
            ),
            measured_at_ns=now,
        )

    def profile_all(self) -> tuple[HorizonProfile, ...]:
        profiles = tuple(self.profile(detector) for detector in sorted(self._durations))
        self.standing.profiles_published += len(profiles)
        return profiles

    def _refuse(self, reason: str) -> str:
        self.standing.refused += 1
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason


def describe_horizon_profiling(profiler: SignalHorizonProfiler) -> dict:
    return {
        "part_id": PART_ID,
        "labels_seen": profiler.standing.labels_seen,
        "claims_recorded": profiler.standing.claims_recorded,
        "profiles_published": profiler.standing.profiles_published,
        "profiles_fitted": profiler.standing.profiles_fitted,
        "detectors_tracked": profiler.standing.detectors_tracked,
        "slowest_median_seconds": profiler.standing.slowest_median_seconds,
        "refused": profiler.standing.refused,
        "by_refusal": dict(profiler.standing.by_refusal),
    }


def run_signal_horizon_profiler(
    profiler: SignalHorizonProfiler, control_socket, read_labels, publish_profiles,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for label in read_labels():
            profiler.observe_label(label)
        publish_profiles(profiler.profile_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    labels = Batch(read=context.bus.reader("training-label"))
    publish_profiles = context.bus.publisher_for("horizon-profile")

    return run_signal_horizon_profiler(
        profiler=SignalHorizonProfiler(
            window=int(context.number("signal_horizon_window")),
            minimum_claims=int(context.number("signal_horizon_minimum_claims")),
        ),
        control_socket=context.control_socket,
        read_labels=labels.payloads,
        publish_profiles=publish_profiles,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
