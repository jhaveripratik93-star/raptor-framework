"""Score the user-supplied cpu_usage sequence [10, 30, 80.2, 80.6, 40, 50, 60]
CORRECTLY: as 7 raw samples from ONE microservice, collected 10 seconds
apart (70 seconds total) -- i.e. all 7 fall inside a single 5-minute
aggregation window, not 7 separate deployments.

This uses the real FeatureStore (gate/features/store.py) to ingest the raw
samples and compute the actual windowed feature the gate would score,
exactly as gate/features/definitions.py declares: cpu_usage uses Mean
aggregation over a 5-minute ("5m") window.

Run from the repo root:
    python scripts/demos/demo_user_cpu_window.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer
from gate.features import FeatureStore

baseline = load_baseline("data/v1_baseline.csv")
model = AnomalyModel.train(baseline)
sla = SlaEngine()
scorer = HealthScorer(model, sla)  # no breach tracker: this is ONE window,
                                    # not multiple deployments -- see note below

store = FeatureStore(redis_client=None)  # memory-only, no cluster needed

component = "user-demo-service"
sequence = [10, 30, 80.2, 80.6, 40, 50, 60]
interval_seconds = 10

print(f"Ingesting {len(sequence)} raw cpu_usage samples, {interval_seconds}s apart "
      f"({(len(sequence)-1)*interval_seconds}s total span -- well inside the 5-minute window):")
for i, cpu in enumerate(sequence):
    ts = i * interval_seconds
    store.ingest(component, {"cpu_usage": float(cpu)}, timestamp=ts)
    print(f"  t={ts:>3}s  cpu_usage={cpu}")

# Advance "now" so the windowed query sees all 7 points (ingest() defaults
# to real wall-clock time; we injected synthetic timestamps above, so query
# the aggregate using the store's own aggregation logic directly for the
# cpu_usage feature at a "now" just after the last sample).
# Monkeypatch the store's clock just for this read, matching the synthetic
# timeline above (t=60s was the last sample).
store._now = lambda: 60.0 + 1.0

aggregated_cpu = store.compute_feature(component, "cpu_usage")
print(f"\nAggregated cpu_usage over the 5-minute window (Mean, per "
      f"gate/features/definitions.py): {aggregated_cpu:.3f}")
print(f"(plain average of the 7 raw values, for comparison: "
      f"{sum(sequence)/len(sequence):.3f})")

# Score ONE sample using that single aggregated value, holding the other 8
# metrics at known-healthy values (row 0 of data/v1_baseline.csv, which
# scores a clean 100.0 anomaly health against this baseline).
sample = dict(
    cpu_usage=aggregated_cpu,
    memory_usage=51.0, pod_restarts=0.0, http_error_rate=0.1,
    p99_latency=81.0, availability=99.9, redis_latency=3.0,
    endpoint_latency=86.0, packet_loss=0.1,
)
verdict = scorer.score(component, sample)

print(f"\nSingle windowed sample scored:")
print(f"  cpu_usage (aggregated) = {aggregated_cpu:.3f}")
print(f"  anomaly_score          = {verdict.anomaly_score}")
print(f"  SLA breach on cpu?     = {any(v.metric == 'cpu_usage' for v in verdict.violations)}")
print(f"  health_score           = {verdict.health_score}")
print(f"  DECISION               = {verdict.decision}")

print("\nNOTE: no persistent-breach tracker used here on purpose -- these 7")
print("readings are ONE deployment's windowed sample, not 7 separate")
print("deployments. gate/signals/trend.py's cross-SAMPLE tracking only applies across")
print("separate scoring calls (e.g. successive 5-min windows / deployments),")
print("not across the raw sub-window readings that get aggregated into one")
print("sample in the first place.")
