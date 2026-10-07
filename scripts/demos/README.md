# Demo scripts

Standalone, narrative scripts that exist purely to produce the worked
examples in `docs/how_the_scoring_actually_works.md`. None of these are part
of operating the gate against a real cluster — see `scripts/README.md` for
those (`capture_baseline.py`, `live_sample_and_score.py`,
`run_production.sh`).

Each script imports and calls the real `gate.*` code (no shortcuts, no
reimplemented logic) and is runnable standalone from the repo root:

```bash
python scripts/demos/demo_baseline_calibration.py
python scripts/demos/demo_cpu_usage_isolation_forest.py
python scripts/demos/demo_penalty_change_impact.py
python scripts/demos/demo_trend_blindspot.py
python scripts/demos/demo_user_cpu_window.py
python scripts/demos/demo_volatility_live.py
```

| Script | What it demonstrates |
|--------|----------------------|
| `demo_baseline_calibration.py` | How `baseline_mean` / `baseline_min` / `tolerance` are derived from `data/v1_baseline_live.csv` |
| `demo_cpu_usage_isolation_forest.py` | The Isolation Forest + SLA engine scoring cpu_usage across a range of values, holding other metrics fixed |
| `demo_penalty_change_impact.py` | How `SLA_BREACH_PENALTY` changes a borderline sample's decision |
| `demo_trend_blindspot.py` | Why signal 3 (`gate/signals/trend.py`) exists: a slow-climbing metric that never crosses its SLA line across separate deployments |
| `demo_user_cpu_window.py` | Why signal 4 (`gate/signals/volatility.py`) exists: a single window's raw ticks that Mean-aggregation hides |
| `demo_volatility_live.py` | Signal 4 exercised end-to-end through the real FastAPI `/ingest` + `/score` endpoints |

`demo_volatility_live.py` requires `fastapi`/`starlette` to import cleanly;
if your environment has a version mismatch between the two, that script will
fail on import before reaching any of its own code.
