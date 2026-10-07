"""Within-window breach-fraction checking.

WHY THIS MODULE EXISTS
  gate/features/definitions.py aggregates most metrics with Mean over a
  5-minute window before the SLA engine or Isolation Forest ever sees them.
  Mean is a reasonable choice for a steady metric, but it has a real blind
  spot: a window where a majority of raw ticks actually breached the SLA can
  still average out to a comfortably "healthy"-looking number.

  Confirmed with a real example: raw cpu_usage ticks
  [50, 50, 80.2, 80.6, 40, 90, 90] -- 4 of 7 ticks (57%) are over the 80% SLA
  line, including two separate back-to-back spikes to 90% -- yet
  Mean([50,50,80.2,80.6,40,90,90]) = 68.7%, which is comfortably under 80 and
  scores a clean PROMOTE through the normal pipeline. The aggregation step
  itself destroyed the information needed to catch this.

  This is a DIFFERENT gap from gate/signals/trend.py's persistent-breach
  tracking. trend.py looks ACROSS separate scored samples (e.g. successive
  deployments or successive 5-minute windows) for a metric that keeps
  crossing its line. This module looks WITHIN a single window's raw ticks,
  before aggregation, for a metric that spent an unacceptable fraction of
  that one window over its line -- a problem Mean-of-the-window can hide
  entirely, even on the very first sample.

WHAT THIS MODULE DOES NOT DO
  It still only looks at ticks ALREADY inside the current window. If a
  metric is climbing but has never crossed its SLA line within any single
  window observed so far, there's nothing here (or in trend.py) to flag it
  -- see docs/how_the_scoring_actually_works.md for that separate, still-
  open limitation (slope/rate-of-change detection on raw values, which
  remains unimplemented).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..metrics import MetricSpec


@dataclass(frozen=True)
class WindowBreach:
    """One metric whose raw ticks breached their SLA too often within a
    single aggregation window, even though the aggregated (Mean/P99/etc.)
    value handed to the SLA engine did not breach."""
    metric: str
    breach_fraction: float   # 0.0-1.0, fraction of raw ticks that breached
    breach_count: int
    sample_count: int
    aggregated_value: float
    detail: str


class WindowVolatilityChecker:
    """Flags a metric whose raw within-window ticks breached their SLA
    threshold on at least `min_fraction` of samples, even when the
    aggregated value (what the SLA engine actually sees) does not breach.

    This does not change or replace the aggregated value used for scoring
    -- it is an independent, additional signal reported alongside the
    normal violations list, exactly like gate/signals/trend.py's persistent
    breaches are reported alongside (not instead of) the ordinary SLA
    violations.
    """

    def __init__(self, min_fraction: float = 0.5, min_samples: int = 3) -> None:
        if not 0.0 < min_fraction <= 1.0:
            raise ValueError("min_fraction must be in (0, 1]")
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        self.min_fraction = min_fraction
        self.min_samples = min_samples

    def check(self, spec: MetricSpec, raw_values: list[float],
              aggregated_value: float) -> WindowBreach | None:
        """Check one metric's raw within-window ticks against its SLA.

        Returns None if there aren't enough samples to judge, or if the
        breach fraction is below `min_fraction`. `aggregated_value` is
        included only for reporting -- it is not re-derived here.
        """
        if len(raw_values) < self.min_samples:
            return None

        breach_count = sum(1 for v in raw_values if spec.is_breach(v))
        fraction = breach_count / len(raw_values)
        if fraction < self.min_fraction:
            return None

        return WindowBreach(
            metric=spec.name,
            breach_fraction=round(fraction, 3),
            breach_count=breach_count,
            sample_count=len(raw_values),
            aggregated_value=aggregated_value,
            detail=(
                f"{spec.name} breached its SLA on {breach_count} of "
                f"{len(raw_values)} raw samples ({fraction*100:.0f}%) within "
                f"this window, even though the aggregated value "
                f"({aggregated_value}{spec.unit}) did not breach -- "
                f"aggregation (Mean) hid a majority-bad window."
            ),
        )

    def check_all(self, specs, raw_values_by_metric: dict[str, list[float]],
                  aggregated_sample: dict[str, float]) -> list[WindowBreach]:
        """Run check() for every metric that has raw values available."""
        results: list[WindowBreach] = []
        for spec in specs:
            raw = raw_values_by_metric.get(spec.name)
            if not raw:
                continue
            result = self.check(spec, raw, aggregated_sample.get(spec.name, float("nan")))
            if result is not None:
                results.append(result)
        return results
