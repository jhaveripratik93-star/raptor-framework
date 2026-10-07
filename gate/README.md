# Raptor Deployment Gate — Phase 2 (Gate Logic)

Pure-Python implementation of the canary health scoring gate. Runs entirely in
**local / CSV mode** — no cluster or live infra required. Production mode
(Phase 3) will swap the CSV loaders for live collectors behind the same
interface.

## What it does

Given a canary's metrics, the gate returns a single **PROMOTE** or **ROLLBACK**
decision built from three signals:

1. **Isolation Forest** trained on the v1 production baseline — detects
   cross-metric anomalies relative to known-good behaviour.
2. **SLA rule engine** — checks 9 explicit thresholds; each breach deducts
   `SLA_BREACH_PENALTY` points (configurable in `gate/__init__.py`; default 20)
   from the health score.
3. **Cross-sample persistent-breach tracking** (`trend.py`) — if the *same*
   metric breaches its SLA across multiple recent samples for a component
   (default: 2 of the last 5), the gate forces ROLLBACK outright, regardless
   of that one sample's health score. This exists specifically for the case
   where a single sample's SLA-breach penalty isn't enough, by itself, to
   fail that sample — but the metric breaking its SLA repeatedly, release
   after release, is itself evidence of a real problem.

Health score (from signals 1+2) is 0-100. `>= 70` promotes, `< 70` rolls back
— unless signal 3 overrides it to ROLLBACK. The process exit code is `0` on
PROMOTE and `1` on ROLLBACK, so any CI/CD tool can gate on it.

Signal 3 is **opt-in**: `HealthScorer(model, sla)` with no `breach_tracker`
behaves identically to the original two-signal design. `gate/cli.py` enables
it by default (backed by a local JSON file, since each CLI invocation is a
fresh process); pass `--no-breach-tracking` to disable it and match the
original per-sample-only behaviour.

## Layout

| File | Responsibility |
|------|----------------|
| `metrics.py` | The 9 metric specs (source, unit, threshold, direction) — single source of truth |
| `sla.py` | SLA rule engine; produces the violations list |
| `model.py` | Isolation Forest wrapper; trains on baseline, maps anomaly to 0-100 |
| `signals/trend.py` | Cross-sample persistent-breach tracking; forces ROLLBACK when one metric keeps breaching |
| `signals/volatility.py` | Within-window breach-fraction checking; forces ROLLBACK when raw ticks breach but aggregation hides it |
| `signals/adaptive.py` | ML-enhanced, opt-in drop-in replacements for the two signals above |
| `scoring.py` | Combines anomaly + SLA + the opt-in signal overrides into the health score and `Verdict` |
| `dataio.py` | CSV loaders for baseline and scenario files (local mode) |
| `cli.py` | Command-line runner |
| `config/production.yaml` | Per-environment SLA thresholds (optional override) |

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux
```

## Run the two demo scenarios

```bash
# Healthy canary -> PROMOTE (exit 0)
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv

# Degraded canary -> ROLLBACK (exit 1), lists 8 SLA breaches
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_degraded.csv

# JSON output for pipeline consumption
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_degraded.csv --json
```

## Tests

```bash
python -m pytest
```

`tests/test_gate.py` covers the document's test table (gate outcomes,
anomaly score present, violations listed, and per-metric SLA thresholds for
all 9 metrics). `tests/test_trend.py` and `tests/test_volatility.py`
separately cover the two opt-in override signals in `gate/signals/`:
counting/windowing logic, all three history-storage backends, and
`HealthScorer`'s forced-ROLLBACK override — including the headline case of
a sample that would PROMOTE on its own health score alone, forced to
ROLLBACK on its second occurrence of the same breach.

## Notes on the demo numbers

The healthy scenario scores ~80 here rather than the document's 93. The exact
number depends on the baseline data; `data/v1_baseline.csv` is a representative
synthetic baseline, not the document's original dataset. What matters — and what
the tests assert — is the **decision**: healthy comfortably clears the gate with
zero SLA breaches (PROMOTE), and the degraded canary floors at 0 with 8 breaches
(ROLLBACK), matching the document's Scenario A / B outcomes exactly. In Phase 3
the baseline will come from real v1 metrics, which will shift the exact score.
