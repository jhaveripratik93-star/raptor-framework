"""Optional, opt-in scoring signals layered on top of the core engine.

The core gate (gate/model.py + gate/sla.py, combined in gate/scoring.py) is
signals 1+2: Isolation Forest anomaly detection plus the SLA rule engine's
per-breach penalty. Everything in this subpackage is an additional,
opt-in override on top of that core -- supplying none of them reproduces
the original two-signal behaviour exactly.

  trend.py      Signal 3: cross-sample persistent-breach tracking.
  volatility.py Signal 4: within-window breach-fraction checking.
  adaptive.py   ML-enhanced, drop-in replacements for signals 3 and 4
                (EWMABreachTracker, AdaptiveVolatilityChecker) -- not wired
                into gate/cli.py or gate/serving/app.py by default.

See docs/how_the_scoring_actually_works.md for the full rationale, worked
examples, and exactly how each signal combines with the others.
"""
