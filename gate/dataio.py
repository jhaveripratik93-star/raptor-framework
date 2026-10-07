"""CSV loading for local/simulation mode.

Local mode (per the framework document) reads metrics from CSV instead of live
infra. These helpers turn CSV rows into the metric-sample dicts the rest of the
gate consumes. Production mode (Phase 3) will swap these for live collectors
behind the same dict interface.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .metrics import METRIC_NAMES


def _coerce_row(row: dict[str, str]) -> dict[str, float]:
    """Convert the metric columns of a CSV row to floats."""
    sample: dict[str, float] = {}
    for name in METRIC_NAMES:
        if name not in row:
            raise KeyError(f"CSV row missing metric column {name!r}")
        sample[name] = float(row[name])
    return sample


def load_baseline(path: str | Path) -> list[dict[str, float]]:
    """Load all rows of a baseline CSV as a list of metric-sample dicts."""
    rows: list[dict[str, float]] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            rows.append(_coerce_row(row))
    if not rows:
        raise ValueError(f"no rows found in baseline CSV {path}")
    return rows


def load_scenario(path: str | Path) -> tuple[str, dict[str, float]]:
    """Load a single-canary scenario CSV.

    Returns (component_name, sample). The optional `component` column names the
    deployment; if absent, the file stem is used.
    """
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"no rows found in scenario CSV {path}")
    row = rows[0]
    component = row.get("component") or Path(path).stem
    return component, _coerce_row(row)
