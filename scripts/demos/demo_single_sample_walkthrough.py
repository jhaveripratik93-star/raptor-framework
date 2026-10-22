"""Demo: every single number behind ONE sample's health score, spelled out.

Picks one real, live-captured sample (sample 1 of
data/scenario_positive_promote.csv) and walks through
every calculation the gate actually performs on it, step by step, printing
each intermediate number -- using the real gate.model / gate.sla /
gate.scoring code, not a re-implementation. Nothing here is approximated;
every number printed is read directly off the real objects
(AnomalyModel._baseline_mean, ._baseline_min, ._tolerance, raw_score(),
anomaly_health(), SlaEngine.evaluate(), HealthScorer.score()).

Run from the repo root:
    python scripts/demos/demo_single_sample_walkthrough.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate import GATE_THRESHOLD, SLA_BREACH_PENALTY
from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer
from gate.metrics import METRICS, feature_vector

BASELINE_PATH = "data/v1_baseline_live.csv"

# Sample 1 from the real live capture
# data/scenario_positive_promote.csv
SAMPLE = dict(
    cpu_usage=0.6561085972850679,
    memory_usage=20.59912929100114,
    pod_restarts=0.0,
    http_error_rate=0.0,
    p99_latency=54.754,
    availability=100.0,
    redis_latency=1.01,
    endpoint_latency=9.917,
    packet_loss=0.0,
)


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main() -> None:
    hr("INPUT: the one sample we are scoring (real, live-captured)")
    for spec in METRICS:
        print(f"  {spec.name:<18s} = {SAMPLE[spec.name]}")

    # ---------------------------------------------------------- STEP 1: train
    hr("STEP 1: train the Isolation Forest on the healthy baseline")
    baseline = load_baseline(BASELINE_PATH)
    print(f"  Baseline file : {BASELINE_PATH}")
    print(f"  Baseline rows : {len(baseline)}")
    model = AnomalyModel.train(baseline)
    print()
    print("  Calibration numbers computed from the baseline (gate/model.py, "
          "AnomalyModel.train):")
    print(f"    baseline_mean = {model._baseline_mean:.6f}   "
          f"(average decision_function() score of the healthy rows)")
    print(f"    baseline_min  = {model._baseline_min:.6f}   "
          f"(the single LEAST-normal healthy row)")
    print(f"    spread        = baseline_mean - baseline_min "
          f"= {model._baseline_mean - model._baseline_min:.6f}")
    print(f"    tolerance     = spread * 4.0 = {model._tolerance:.6f}   "
          f"(how much worse than baseline_mean a sample can score before "
          f"its anomaly_health hits 0)")

    # --------------------------------------------------- STEP 2: feature vector
    hr("STEP 2: turn the sample dict into the ordered feature vector")
    vec = feature_vector(SAMPLE)
    print("  gate/metrics.py: feature_vector() reads the 9 values in the exact")
    print("  canonical METRICS order (this order is fixed -- same order the")
    print("  model was trained on):")
    for spec, val in zip(METRICS, vec):
        print(f"    [{METRICS.index(spec)}] {spec.name:<18s} = {val}")

    # ----------------------------------------------- STEP 3: raw decision_function
    hr("STEP 3: run the trained model on this one feature vector")
    raw = model.raw_score(SAMPLE)
    print("  gate/model.py: AnomalyModel.raw_score() calls sklearn's")
    print("  IsolationForest.decision_function() on this single point.")
    print(f"  raw decision_function score = {raw:.6f}")
    print("  (higher = looks more normal vs. the baseline; lower = less normal)")

    # ------------------------------------------------------- STEP 4: deficit
    hr("STEP 4: deficit = how far BELOW baseline_mean this sample scored")
    deficit = model._baseline_mean - raw
    print(f"  deficit = baseline_mean - raw")
    print(f"          = {model._baseline_mean:.6f} - ({raw:.6f})")
    print(f"          = {deficit:.6f}")
    if deficit <= 0:
        print("  deficit <= 0  ->  this sample is AT LEAST as normal as the")
        print("  baseline average  ->  anomaly_health = 100.0 immediately")
    else:
        print("  deficit > 0  ->  this sample scored WORSE than the baseline")
        print("  average -> anomaly_health falls off below 100, scaled by tolerance")

    # ----------------------------------------------- STEP 5: anomaly_health 0-100
    hr("STEP 5: convert deficit into the 0-100 anomaly_health score")
    anomaly_health = model.anomaly_health(SAMPLE)
    print("  gate/model.py: AnomalyModel.anomaly_health()")
    print("    if deficit <= 0:   anomaly_health = 100.0")
    print("    else:              anomaly_health = 100.0 * (1 - deficit / tolerance)")
    print("                       clamped into [0, 100]")
    if deficit > 0:
        unclamped = 100.0 * (1.0 - deficit / model._tolerance)
        print(f"    = 100.0 * (1 - {deficit:.6f} / {model._tolerance:.6f})")
        print(f"    = 100.0 * (1 - {deficit / model._tolerance:.6f})")
        print(f"    = {unclamped:.6f}  ->  clamped -> {anomaly_health:.1f}")
    else:
        print(f"    -> anomaly_health = {anomaly_health:.1f}")

    is_anomaly = model.is_anomaly(SAMPLE)
    print()
    print(f"  (separate, informational-only flag) model.is_anomaly() = {is_anomaly}")
    print("  This is sklearn's own predict() -1/+1 call -- a DIFFERENT, binary")
    print("  signal. It does NOT feed into health_score or the decision at all;")
    print("  it is only reported alongside the verdict for visibility.")

    # --------------------------------------------------------- STEP 6: SLA check
    hr("STEP 6: check every metric against its fixed SLA threshold")
    sla = SlaEngine()
    violations = sla.evaluate(SAMPLE)
    print(f"  {'metric':<18s} {'value':>12s}  {'rule':<10s}  {'breach?':>8s}")
    for spec in METRICS:
        val = SAMPLE[spec.name]
        breach = spec.is_breach(val)
        op = "<=" if spec.direction == "max" else ">="
        rule = f"{op} {spec.threshold}{spec.unit}"
        print(f"  {spec.name:<18s} {val:>12.4f}  {rule:<10s}  {str(breach):>8s}")
    print()
    print(f"  Total SLA breaches this sample: {len(violations)}")
    for v in violations:
        print(f"    - {v.detail}")

    # ----------------------------------------------------- STEP 7: deductions
    hr("STEP 7: subtract a fixed penalty per breach (gate/__init__.py)")
    print(f"  SLA_BREACH_PENALTY = {SLA_BREACH_PENALTY}  (points lost per broken rule)")
    deductions = SLA_BREACH_PENALTY * len(violations)
    print(f"  deductions = {SLA_BREACH_PENALTY} * {len(violations)} breach(es) = {deductions}")

    # ------------------------------------------------------- STEP 8: health score
    hr("STEP 8: combine anomaly_health and the SLA deductions")
    health = max(0.0, min(100.0, anomaly_health - deductions))
    health = round(health, 1)
    print(f"  health_score = clamp( anomaly_health - deductions, 0, 100 )")
    print(f"               = clamp( {anomaly_health:.1f} - {deductions}, 0, 100 )")
    print(f"               = clamp( {anomaly_health - deductions:.1f}, 0, 100 )")
    print(f"               = {health}")

    # ------------------------------------------------------- STEP 9: decision
    hr("STEP 9: compare against the gate threshold")
    print(f"  GATE_THRESHOLD = {GATE_THRESHOLD}  (gate/__init__.py)")
    decision = "PROMOTE" if health >= GATE_THRESHOLD else "ROLLBACK"
    print(f"  decision = PROMOTE  if health_score >= {GATE_THRESHOLD}")
    print(f"             ROLLBACK otherwise")
    print(f"  health_score = {health}  {'>=' if health >= GATE_THRESHOLD else '<'} {GATE_THRESHOLD}")
    print(f"  -> decision (signals 1+2 only) = {decision}")

    # -------------------------------------------- STEP 10: verify via the real API
    hr("STEP 10: cross-check against the real HealthScorer.score() call")
    scorer = HealthScorer(model, sla)  # no trend/volatility trackers -- signals 1+2 only
    verdict = scorer.score("walkthrough-sample", SAMPLE)
    print("  Calling the ACTUAL gate.scoring.HealthScorer.score() -- same inputs,")
    print("  no shortcuts -- to confirm every manual step above matches exactly:")
    print(f"    verdict.anomaly_score = {verdict.anomaly_score}   "
          f"(manual calc: {anomaly_health:.1f}) {'MATCH' if round(anomaly_health,1)==verdict.anomaly_score else 'MISMATCH'}")
    print(f"    verdict.health_score  = {verdict.health_score}   "
          f"(manual calc: {health}) {'MATCH' if health==verdict.health_score else 'MISMATCH'}")
    print(f"    verdict.decision      = {verdict.decision}   "
          f"(manual calc: {decision}) {'MATCH' if decision==verdict.decision else 'MISMATCH'}")

    # --------------------------------------------------- STEP 11: what moves it
    hr("WHAT WOULD FLIP THIS TO ROLLBACK -- the levers, in order of directness")
    print("  1. Cross an SLA threshold (gate/metrics.py) -- most direct lever.")
    print(f"     Each breach costs a flat {SLA_BREACH_PENALTY} points. From "
          f"health_score={health}, it takes")
    breaches_needed = 0
    simulated = health
    while simulated >= GATE_THRESHOLD:
        simulated -= SLA_BREACH_PENALTY
        breaches_needed += 1
    print(f"     {breaches_needed} breach(es) of ANY metric to push health_score below "
          f"{GATE_THRESHOLD} (since each one is a flat -{SLA_BREACH_PENALTY}).")
    print()
    print("  2. Move far enough from the baseline that anomaly_health itself drops")
    print("     -- requires the raw feature vector to land in a region the trained")
    print("     Isolation Forest never saw as healthy. This is NOT a single-column")
    print("     lever like #1; it depends on the whole 9-dimensional point's")
    print("     distance from the baseline cloud, scaled by `tolerance` above.")
    print()
    print("  3. (not active in this walkthrough -- no tracker/raw_values supplied)")
    print("     Repeat the SAME metric's breach on >=2 of the last 5 samples for")
    print("     this component (gate/signals/trend.py) -- forces ROLLBACK outright,")
    print("     regardless of health_score, no matter how high it is.")
    print()
    print("  4. (not active in this walkthrough -- needs raw per-tick data)")
    print("     Have the SAME metric breach its SLA on >=50% of the raw ticks")
    print("     inside one aggregation window (gate/signals/volatility.py) --")
    print("     also forces ROLLBACK outright, bypassing health_score entirely.")


if __name__ == "__main__":
    main()
