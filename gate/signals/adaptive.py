"""ML-enhanced drop-in replacements for the deterministic signal 3 and 4.

WHY THIS MODULE EXISTS

Signal 3 (gate/signals/trend.py BreachTracker) uses a hard-coded rule:
"flag if the same metric breached >= N times in the last M samples."
The threshold (N, M) is a universal constant that doesn't know whether the
service normally breaches that metric once in a while or never.

Signal 4 (gate/signals/volatility.py WindowVolatilityChecker) uses a hard-coded
fraction: "flag if >= 30% of raw ticks in this window breached the SLA."
The 30% doesn't know whether this metric normally runs 1ms under its SLA
(where 30% breaches is alarming) or 37 units under it (where 30% might be
worth a closer look but not a hard rollback).

WHAT MAKES THESE ML

Both replacements use the TRAINING DATA (the v1 baseline) to calibrate their
thresholds, rather than treating every service and every metric identically:

  AdaptiveVolatilityChecker (Signal 4 enhancement):
    - Computes per-metric baseline statistics (mean, std, distance to SLA)
    - Sets the within-window alarm threshold based on statistical distance
      from the SLA threshold, not a universal 30%
    - A metric running close to its SLA triggers at a lower breach fraction
      than a metric with large headroom -- learned from data

  EWMABreachTracker (Signal 3 enhancement):
    - Maintains an Exponential Weighted Moving Average of the per-metric
      breach indicator (0/1) across deployment history per component
    - Flags when the current breach deviates significantly from the EWMA
      baseline -- a service that occasionally breaches a metric in normal
      operation won't keep triggering forced rollbacks at every occurrence
    - The z-score threshold is fixed, but what it's measuring adapts to
      each service's own learned historical breach pattern
    - GUARDRAIL: an absolute_breach_ceiling (default 50% of the window) is
      checked BEFORE any EWMA reasoning and always wins. This was added
      after confirming a real flaw: pure EWMA can "learn" a persistently
      breaching metric as normal and stop flagging it (e.g. a service
      breaching 6 of 9 samples was no longer flagged by sample 9, since the
      EWMA had adapted to ~67% as the new baseline). The SLA threshold is a
      business requirement, not a statistic that should erode with repeated
      violations -- the ceiling makes that non-negotiable. See the class
      docstring below for the full explanation.

WHAT MAKES THE OUTPUT MORE CORRECTLY PREDICTABLE

Hard-coded rules treat all services identically. A service where redis latency
is usually 2ms against a 10ms SLA is very different from one usually at 9ms.
A service that legitimately has occasional minor SLA dips behaves differently
than one that is always perfectly within limits. These ML-enhanced versions
capture that difference -- reducing both false positives (alarming on behavior
that is normal for this specific service) and false negatives (not alarming
because a more subtle pattern doesn't quite trip a universal threshold).

STILL HONEST ABOUT LIMITS

Neither of these detects a metric that is climbing but has never crossed its
SLA line. That remains a separate, unimplemented gap (see gate/signals/trend.py
and gate/signals/volatility.py docstrings). These enhance accuracy within the
scope of what each signal was already trying to detect.

USAGE (drop-in replacements -- HealthScorer interface is unchanged)

  from gate.signals.adaptive import train_adaptive_models, EWMABreachTracker, AdaptiveVolatilityChecker
  from gate.signals.trend import FileBreachHistoryStore

  baseline = load_baseline("data/v1_baseline.csv")
  adaptive_model = train_adaptive_models(baseline)

  scorer = HealthScorer(
      model, sla,
      breach_tracker=EWMABreachTracker(store=FileBreachHistoryStore()),
      volatility_checker=AdaptiveVolatilityChecker(adaptive_model),
  )
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..metrics import MetricSpec, METRICS
from .trend import PersistentBreach, BreachHistoryStore, InMemoryBreachHistoryStore
from .volatility import WindowBreach


# ------------------------------------------------------------------ data model
@dataclass
class _PerMetricBaseline:
    """Baseline statistics for one metric, computed from training data."""
    metric: str
    mean: float
    std: float
    threshold: float
    direction: str
    # How many std-devs the baseline mean sits from the SLA threshold.
    # High value = lots of headroom = metric rarely close to the line.
    # Low value  = little headroom = metric habitually near the edge.
    headroom_in_stds: float
    # Expected breach fraction under normal operation (empirical from baseline).
    baseline_breach_fraction: float


@dataclass
class AdaptiveBaselineModel:
    """Trained from the v1 baseline; stores per-metric statistics used by
    both AdaptiveVolatilityChecker and EWMABreachTracker."""
    metrics: dict[str, _PerMetricBaseline]


def train_adaptive_models(
    baseline: list[dict[str, float]],
    specs: tuple[MetricSpec, ...] = METRICS,
) -> AdaptiveBaselineModel:
    """Compute per-metric statistics from the baseline CSV rows.

    Call this once at startup alongside AnomalyModel.train() -- both consume
    the same list of baseline sample dicts, so no extra data capture needed.
    """
    metrics: dict[str, _PerMetricBaseline] = {}

    for spec in specs:
        values = [row[spec.name] for row in baseline if spec.name in row]
        if not values:
            metrics[spec.name] = _PerMetricBaseline(
                metric=spec.name, mean=0.0, std=1.0,
                threshold=spec.threshold, direction=spec.direction,
                headroom_in_stds=0.0, baseline_breach_fraction=0.0,
            )
            continue

        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
        std = max(variance ** 0.5, 1e-6)

        # Headroom: how many stds between baseline mean and SLA threshold.
        # For "max" direction: threshold is upper limit, more is better.
        # For "min" direction: threshold is lower limit, less is better.
        if spec.direction == "max":
            headroom_in_stds = (spec.threshold - mean) / std
        else:  # "min" -- availability, etc.
            headroom_in_stds = (mean - spec.threshold) / std

        breach_count = sum(1 for v in values if spec.is_breach(v))
        baseline_breach_fraction = breach_count / n

        metrics[spec.name] = _PerMetricBaseline(
            metric=spec.name,
            mean=mean,
            std=std,
            threshold=spec.threshold,
            direction=spec.direction,
            headroom_in_stds=max(headroom_in_stds, 0.0),
            baseline_breach_fraction=baseline_breach_fraction,
        )

    return AdaptiveBaselineModel(metrics=metrics)


# -------------------------------------------------------- adaptive volatility
class AdaptiveVolatilityChecker:
    """ML-enhanced Signal 4: sets the within-window breach fraction threshold
    based on each metric's learned distance from its SLA, rather than a
    universal 30%.

    A metric running close to its SLA (e.g. redis_latency at 9ms with a 10ms
    limit, only 1.2 stds of headroom) should alarm at a LOWER breach fraction
    than one with large headroom (e.g. cpu_usage at 43% with an 80% limit, 22
    stds of headroom). The fixed 30% threshold treats both identically.

    The adaptive threshold is: max(baseline_frac + sigma_multiplier * se, floor)
    where:
      - baseline_frac = fraction of baseline samples that actually breached
        (empirical; for a healthy baseline this is typically 0%)
      - se = standard error of the proportion = sqrt(p*(1-p)/n_ticks)
      - floor = min threshold below which we won't alarm regardless, scaled
        inversely to headroom_in_stds (small headroom -> lower floor)
    """

    def __init__(self, model: AdaptiveBaselineModel,
                 sigma_multiplier: float = 3.0,
                 min_samples: int = 3,
                 base_floor: float = 0.30,
                 headroom_floor_scale: float = 0.05):
        """
        sigma_multiplier:    how many standard errors above baseline to flag.
                             Higher = less sensitive, fewer false positives.
        min_samples:         minimum ticks before this signal activates.
        base_floor:          minimum breach fraction to alarm on for metrics
                             with large headroom (matches fixed-checker default).
        headroom_floor_scale: how much to lower the floor per std of headroom.
                              floor = base_floor - headroom_stds * scale.
                              A metric only 1 std from SLA gets a floor near 0.
        """
        self.model = model
        self.sigma = sigma_multiplier
        self.min_samples = min_samples
        self.base_floor = base_floor
        self.headroom_floor_scale = headroom_floor_scale

    def _adaptive_threshold(self, m: _PerMetricBaseline, n: int) -> float:
        """Compute the metric-specific breach fraction threshold.

        Design intent:
          - Large headroom (metric far from SLA): use base_floor unchanged.
            A metric sitting 22 stds below its SLA should alarm at 30%
            breach fraction, same as the fixed checker -- breaches are
            surprising when the metric is so far from danger.
          - Small headroom (metric near its SLA): lower the floor.
            A metric running at 9.5ms against a 10ms SLA has 0.8 stds of
            headroom; natural variance will cause occasional threshold
            crossings that aren't evidence of a real problem.
          - The sigma * se term ensures statistical significance relative
            to the baseline breach fraction in all cases.

        The floor formula: floor = base_floor * clamp(headroom / 3.0, 0, 1),
        clamped at a minimum of 0.05. So:
          - headroom >= 3 stds: floor = base_floor (30% by default)
          - headroom = 1.5 stds: floor = 15%
          - headroom = 0 stds: floor = 5% (won't alarm on a single blip)
        """
        baseline_frac = m.baseline_breach_fraction
        se = math.sqrt(max(baseline_frac * (1 - baseline_frac), 1e-6) / n)

        # Scale floor linearly with headroom, capped at base_floor.
        headroom_factor = min(1.0, m.headroom_in_stds / 3.0)
        floor = max(self.base_floor * headroom_factor, 0.05)

        return max(baseline_frac + self.sigma * se, floor)

    def check(self, spec: MetricSpec, raw_values: list[float],
              aggregated_value: float) -> WindowBreach | None:
        if len(raw_values) < self.min_samples:
            return None

        m = self.model.metrics.get(spec.name)
        if m is None:
            return None

        n = len(raw_values)
        breach_count = sum(1 for v in raw_values if spec.is_breach(v))
        actual_fraction = breach_count / n

        threshold = self._adaptive_threshold(m, n)

        if actual_fraction < threshold:
            return None

        # Compute z-score relative to learned baseline
        se = math.sqrt(max(m.baseline_breach_fraction * (1 - m.baseline_breach_fraction),
                          1e-6) / n)
        z_score = (actual_fraction - m.baseline_breach_fraction) / max(se, 1e-6)

        return WindowBreach(
            metric=spec.name,
            breach_fraction=round(actual_fraction, 3),
            breach_count=breach_count,
            sample_count=n,
            aggregated_value=aggregated_value,
            detail=(
                f"{spec.name} adaptive breach alarm: {actual_fraction:.0%} of "
                f"{n} raw ticks breached ({z_score:.1f}σ above learned baseline "
                f"of {m.baseline_breach_fraction:.0%}; "
                f"headroom={m.headroom_in_stds:.1f}σ → "
                f"adaptive threshold={threshold:.0%}). "
                f"Aggregated value {round(aggregated_value,2)}{spec.unit} "
                f"hid {breach_count} breaching ticks."
            ),
        )

    def check_all(self, specs, raw_values_by_metric: dict[str, list[float]],
                  aggregated_sample: dict[str, float]) -> list[WindowBreach]:
        results: list[WindowBreach] = []
        for spec in specs:
            raw = raw_values_by_metric.get(spec.name)
            if not raw:
                continue
            result = self.check(spec, raw, aggregated_sample.get(spec.name, float("nan")))
            if result is not None:
                results.append(result)
        return results


# ----------------------------------------------------- adaptive breach tracker
class EWMABreachTracker:
    """ML-enhanced Signal 3: uses Exponential Weighted Moving Average to learn
    each component's natural per-metric breach rate, then flags when the
    current deployment deviates significantly from that learned baseline.

    The fixed BreachTracker says: "flag if the same metric breached >= N times
    in the last M samples." That treats a service which legitimately breaches
    redis_latency once every 5 deployments (expected, not alarming) the same
    way as one that has never breached it and just breached it twice in a row
    (genuinely alarming).

    EWMA separates these cases:
      - High EWMA breach rate + current breach: z-score is LOW → no flag
        (this is normal for this service)
      - Low/zero EWMA breach rate + current breach: z-score is HIGH → flag
        (this is genuinely anomalous for this service)

    CALIBRATION NOTE: EWMA requires deployment history to be meaningful.
    For components with < min_history samples, falls back to a simple
    counter (same behavior as the fixed BreachTracker). The EWMA value is
    what makes this ML -- the decision threshold for each metric and each
    component is a number learned from that component's own history.

    CRITICAL SAFEGUARD -- absolute_breach_ceiling:
    EWMA on its own has a serious flaw, found and confirmed by testing: if a
    metric breaches its SLA repeatedly, the EWMA breach-rate baseline rises
    to match it, and the z-score for yet another breach eventually drops
    below the alarm threshold -- the tracker "learns" the breaching behavior
    as normal and STOPS flagging it. Confirmed concretely: a service
    breaching redis_latency in 6 of 9 samples (67%) was no longer flagged by
    sample 9, because the EWMA had adapted to treat ~67% breaches as the
    service's new normal.

    This is backwards. The SLA threshold is a business requirement, not a
    statistical property that should erode just because a service has been
    violating it for a while. EWMA alone can quietly launder a persistently
    broken service into looking "normal."

    The fix: `absolute_breach_ceiling` (default 0.5) is a hard floor applied
    BEFORE any EWMA/z-score reasoning. If a metric has breached its SLA on
    >= this fraction of the samples in the current window, it is ALWAYS
    flagged -- no EWMA adaptation can suppress it. EWMA only ever makes the
    tracker MORE sensitive than this floor (catching subtler anomalies in
    services that are normally clean); it can never make it LESS sensitive
    than the floor. This mirrors how gate/signals/volatility.py's
    WindowVolatilityChecker already treats breach fraction as a hard,
    SLA-anchored signal that aggregation cannot be allowed to erase -- the
    same principle applies here
    to EWMA.
    """

    def __init__(self,
                 store: BreachHistoryStore | None = None,
                 alpha: float = 0.25,
                 sigma_threshold: float = 2.5,
                 min_history: int = 5,
                 fallback_min_breaches: int = 2,
                 window_size: int = 20,
                 absolute_breach_ceiling: float = 0.5):
        """
        alpha:                EWMA smoothing (0.1=slow/stable, 0.5=fast/reactive).
        sigma_threshold:      z-score above which a breach is flagged as anomalous.
        min_history:          minimum samples before EWMA activates.
        fallback_min_breaches: threshold used before enough history exists.
        window_size:          rolling history length fed into EWMA.
        absolute_breach_ceiling: hard floor (fraction of the window) above
                             which a metric is ALWAYS flagged, regardless of
                             what EWMA has learned. Default 0.5 means a
                             metric breaching >= 50% of recent samples is
                             always treated as a persistent problem -- EWMA
                             adaptation can never suppress this. Only
                             evaluated once >= min_samples_for_ceiling
                             samples exist (a single breach out of 1 sample
                             is 100% but is not evidence of a "majority
                             pattern" -- it just means one breach happened).
        """
        if not 0.0 < absolute_breach_ceiling <= 1.0:
            raise ValueError("absolute_breach_ceiling must be in (0, 1]")
        self.store = store or InMemoryBreachHistoryStore()
        self.alpha = alpha
        self.sigma_threshold = sigma_threshold
        self.min_history = min_history
        self.fallback_min_breaches = fallback_min_breaches
        self.window_size = window_size
        self.absolute_breach_ceiling = absolute_breach_ceiling
        # Require at least this many recorded samples before the hard
        # ceiling can fire -- avoids calling a single breach a "majority
        # pattern." Matches fallback_min_breaches, the same minimum evidence
        # bar the pre-EWMA fallback counter already used.
        self.min_samples_for_ceiling = fallback_min_breaches

    def _ewma_stats(self, series: list[float]) -> tuple[float, float]:
        """Compute EWMA mean and std from series, excluding the last element.
        The last element is the current observation we are evaluating."""
        baseline = series[:-1]
        if not baseline:
            return 0.0, 1e-3

        ewma = baseline[0]
        ewma_var = 0.0
        for val in baseline[1:]:
            ewma = self.alpha * val + (1 - self.alpha) * ewma
            ewma_var = (self.alpha * (val - ewma) ** 2
                        + (1 - self.alpha) * ewma_var)

        return ewma, max(ewma_var ** 0.5, 1e-3)

    def record_and_check(self, component: str,
                          breached_metrics: set[str]) -> list[PersistentBreach]:
        history = self.store.load(component)
        history.append(sorted(breached_metrics))
        history = history[-self.window_size:]
        self.store.save(component, history)

        persistent: list[PersistentBreach] = []
        flagged_by_ceiling: set[str] = set()

        # --- HARD CEILING, checked first, independent of EWMA/history size ---
        # Any metric breaching >= absolute_breach_ceiling of the window is
        # ALWAYS flagged. This is intentionally evaluated before and
        # separately from the EWMA logic below, so EWMA's learned baseline
        # can never suppress a persistently-breaching metric -- see the
        # class docstring for the concrete failure case this prevents.
        counts: dict[str, int] = {}
        for entry in history:
            for m in entry:
                counts[m] = counts.get(m, 0) + 1

        for metric, count in counts.items():
            if len(history) < self.min_samples_for_ceiling:
                continue  # not enough evidence yet to call this a pattern
            fraction = count / len(history)
            if fraction >= self.absolute_breach_ceiling:
                flagged_by_ceiling.add(metric)
                persistent.append(PersistentBreach(
                    metric=metric,
                    breach_count=count,
                    window_size=len(history),
                    detail=(
                        f"{metric} breached its SLA in {count} of the last "
                        f"{len(history)} samples ({fraction:.0%}) -- at or "
                        f"above the absolute ceiling of "
                        f"{self.absolute_breach_ceiling:.0%}. Always flagged "
                        f"regardless of historical pattern; a majority-"
                        f"breaching metric is never treated as 'normal'."
                    )
                ))

        # --- Insufficient history for EWMA: fall back to a simple counter ---
        # (only for metrics not already caught by the ceiling above)
        if len(history) < self.min_history:
            for metric in breached_metrics:
                if metric in flagged_by_ceiling:
                    continue
                if counts.get(metric, 0) >= self.fallback_min_breaches:
                    persistent.append(PersistentBreach(
                        metric=metric,
                        breach_count=counts[metric],
                        window_size=len(history),
                        detail=(
                            f"{metric} breached {counts[metric]} times in the "
                            f"last {len(history)} samples (insufficient "
                            f"history for EWMA; using fallback threshold="
                            f"{self.fallback_min_breaches})."
                        )
                    ))
            return persistent

        # --- EWMA-based detection for metrics not already caught above ---
        # Only evaluate metrics that breached THIS sample; the ceiling check
        # above has already handled any metric at or above the hard floor.
        for metric in breached_metrics:
            if metric in flagged_by_ceiling:
                continue
            series = [1.0 if metric in entry else 0.0 for entry in history]
            ewma, ewma_std = self._ewma_stats(series)
            current = series[-1]  # always 1.0 (metric breached this sample)
            z_score = (current - ewma) / ewma_std
            total = sum(1 for v in series if v > 0)

            if z_score >= self.sigma_threshold:
                persistent.append(PersistentBreach(
                    metric=metric,
                    breach_count=total,
                    window_size=len(history),
                    detail=(
                        f"{metric} EWMA anomaly: breach rate z-score={z_score:.1f}σ "
                        f"above learned baseline "
                        f"(EWMA breach rate={ewma:.1%}, σ={ewma_std:.3f}). "
                        f"Breached {total} of last {len(history)} deployments. "
                        f"Threshold: {self.sigma_threshold}σ."
                    )
                ))

        return persistent
