"""Health scoring and the gate decision.

Combines FOUR signals (as of the persistent-breach + window-volatility
rework -- see gate/signals/trend.py, gate/signals/volatility.py, and
docs/how_the_scoring_actually_works.md for the full rationale and the
worked numeric examples that motivated each one):

  1. Isolation Forest anomaly detection (cross-metric, catches subtle
     regressions no single threshold flags) -- model.py.
  2. The SLA rule engine (9 explicit thresholds; each breach deducts a fixed
     penalty from this one sample's score) -- sla.py.
  3. Cross-sample persistent-breach tracking (did any ONE metric break its
     SLA repeatedly across recent SAMPLES/deployments, even if no single
     sample's point penalty alone was enough to fail it?) -- trend.py.
  4. Within-window breach-fraction checking (did any ONE metric breach its
     SLA on a large fraction of the raw ticks INSIDE one window, even
     though Mean-aggregating those ticks into a single value hid it?) --
     volatility.py.

Signals 3 and 4 look similar but catch different things: 3 looks ACROSS
samples (repeated breaches over time, e.g. successive deployments); 4 looks
WITHIN one sample's raw sub-window data (a majority-bad window that
aggregation smoothed into a healthy-looking single number). A real example
that only signal 4 catches: raw cpu_usage ticks
[50, 50, 80.2, 80.6, 40, 90, 90] average to 68.7% (comfortably under the 80%
SLA) despite 4 of 7 ticks (57%) actually breaching -- Mean aggregation
destroys that information before the SLA engine or the model ever see it.

The final health score (from signals 1+2) is on 0-100. Score >= gate
threshold (default 70) => PROMOTE, otherwise ROLLBACK -- UNLESS signal 3 or
4 fires, in which case the decision is forced to ROLLBACK regardless of the
health score. These are deliberate overrides, not additional deductions: a
metric that keeps crossing its line -- whether release after release (3) or
within a single window's raw data (4) -- is treated as proof of a real
problem, and no amount of "the rest looks fine" is allowed to outvote that.

Design note on how signals 1+2 combine (unchanged from the original design):
  - We start from the model's anomaly-health component (0-100).
  - Each SLA breach subtracts SLA_BREACH_PENALTY points.
  - The score is clamped to [0, 100].

Design note on signals 3 and 4 (both opt-in overrides):
  - Signal 3 only applies when a BreachTracker is supplied.
  - Signal 4 only applies when BOTH a WindowVolatilityChecker is supplied
    AND raw per-tick values are passed into score() via `raw_values`.
  - Without either, behaviour is 100% identical to the original two-signal
    design -- both are additive, opt-in changes, not breaking ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import GATE_THRESHOLD, SLA_BREACH_PENALTY
from .model import AnomalyModel
from .sla import SlaEngine, Violation
from .signals.trend import BreachTracker, PersistentBreach
from .signals.volatility import WindowVolatilityChecker, WindowBreach


@dataclass
class Verdict:
    component: str
    decision: str            # "PROMOTE" or "ROLLBACK"
    health_score: float      # 0-100
    anomaly_score: float     # 0-100 anomaly-health component
    is_anomaly: bool
    violations: list[Violation] = field(default_factory=list)
    gate_threshold: int = GATE_THRESHOLD
    persistent_breaches: list[PersistentBreach] = field(default_factory=list)
    window_breaches: list[WindowBreach] = field(default_factory=list)
    forced_rollback: bool = False
    """True when `decision` was forced to ROLLBACK by the persistent-breach
    tracker (signal 3) and/or the window-volatility checker (signal 4)
    rather than by the health_score/gate_threshold comparison (signals 1+2).
    See `persistent_breaches` / `window_breaches` for which metric(s)
    triggered it."""

    @property
    def promote(self) -> bool:
        return self.decision == "PROMOTE"

    @property
    def exit_code(self) -> int:
        """0 on PROMOTE, 1 on ROLLBACK — for CI/CD pipelines to act on."""
        return 0 if self.promote else 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["violations"] = [asdict(v) for v in self.violations]
        d["persistent_breaches"] = [asdict(p) for p in self.persistent_breaches]
        d["window_breaches"] = [asdict(w) for w in self.window_breaches]
        return d


class HealthScorer:
    """Produces a Verdict from a canary metric sample."""

    def __init__(self, model: AnomalyModel, sla: SlaEngine | None = None,
                 gate_threshold: int = GATE_THRESHOLD,
                 breach_penalty: int = SLA_BREACH_PENALTY,
                 breach_tracker: BreachTracker | None = None,
                 volatility_checker: WindowVolatilityChecker | None = None):
        self.model = model
        self.sla = sla or SlaEngine()
        self.gate_threshold = gate_threshold
        self.breach_penalty = breach_penalty
        self.breach_tracker = breach_tracker
        self.volatility_checker = volatility_checker

    def score(self, component: str, sample: dict[str, float],
              raw_values: dict[str, list[float]] | None = None) -> Verdict:
        """Score one sample.

        `raw_values`, if provided, maps metric name -> the raw per-tick
        values observed inside this sample's aggregation window (e.g. every
        10-second cpu_usage reading inside a 5-minute window), BEFORE
        Mean/P99/etc. aggregation collapsed them into `sample[metric]`. Only
        used by signal 4 (gate/signals/volatility.py); omit it to skip that check
        entirely (e.g. in local/CSV mode, where no raw sub-window data
        exists -- the CSV already contains one aggregated value per row).
        """
        anomaly_health = self.model.anomaly_health(sample)
        is_anomaly = self.model.is_anomaly(sample)
        violations = self.sla.evaluate(sample)

        deductions = self.breach_penalty * len(violations)
        health = max(0.0, min(100.0, anomaly_health - deductions))
        health = round(health, 1)

        decision = "PROMOTE" if health >= self.gate_threshold else "ROLLBACK"

        persistent_breaches: list[PersistentBreach] = []
        if self.breach_tracker is not None:
            breached_metrics = {v.metric for v in violations}
            persistent_breaches = self.breach_tracker.record_and_check(
                component, breached_metrics
            )

        window_breaches: list[WindowBreach] = []
        if self.volatility_checker is not None and raw_values:
            window_breaches = self.volatility_checker.check_all(
                self.sla.specs, raw_values, sample
            )

        forced_rollback = False
        if (persistent_breaches or window_breaches) and decision != "ROLLBACK":
            forced_rollback = True
        if persistent_breaches or window_breaches:
            decision = "ROLLBACK"

        return Verdict(
            component=component,
            decision=decision,
            health_score=health,
            anomaly_score=round(anomaly_health, 1),
            is_anomaly=is_anomaly,
            violations=violations,
            gate_threshold=self.gate_threshold,
            persistent_breaches=persistent_breaches,
            window_breaches=window_breaches,
            forced_rollback=forced_rollback,
        )
