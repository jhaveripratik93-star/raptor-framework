"""Render a client-facing PNG dashboard from a live_sample_and_score.py run.

Reads the CSV that `scripts/live_sample_and_score.py` writes (one row per
live sample: timestamp, all 9 raw metrics, and the real gate's verdict) and
produces a single PNG with:

  1. A 3x3 grid, one subplot per monitored metric, each with its SLA
     threshold drawn as a dashed line and any breaching points marked red.
  2. A bottom panel plotting health_score / anomaly_score over the same
     timeline, with the gate threshold line, PROMOTE/ROLLBACK markers, and
     a naive "SLA-only" decision line for comparison.
  3. Vertical shaded bands at every sample where the real gate's decision
     diverged from what a naive SLA-only monitor would have said -- this is
     the "what would have happened without the ML model" story: a thin
     static-threshold check would have missed (or wrongly flagged) exactly
     these points.

This script never talks to the cluster itself -- it only reads a CSV
already produced by `scripts/live_sample_and_score.py` (or any CSV with the
same columns). Safe to re-run against the same file as many times as needed
for a demo.

Usage:
    python scripts/plot_dashboard.py --csv data/live_results_eric-cenx-rest-api_20261016_143000.csv
    python scripts/plot_dashboard.py --csv <path> --out <path>.png --title "Client Demo"
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")  # headless-safe: never requires a display
import matplotlib.pyplot as plt  # noqa: E402

from gate.metrics import METRICS  # noqa: E402
from gate import GATE_THRESHOLD  # noqa: E402


def _read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"no rows found in {csv_path}")
    return rows


def render_dashboard(csv_path: str | Path, out_path: str | Path | None = None,
                      title: str | None = None) -> Path:
    """Build the dashboard PNG from a live-results CSV and return its path."""
    csv_path = Path(csv_path)
    rows = _read_rows(csv_path)

    x = list(range(1, len(rows) + 1))
    x_labels = [r.get("timestamp", str(i)) for i, r in zip(x, rows)]

    # 1-based sample numbers (matches the x axis / CSV "sample" column) of
    # every row where the real gate's decision diverged from a naive
    # SLA-only monitor's decision.
    divergence_idx = [
        i for i, r in enumerate(rows, start=1)
        if str(r.get("divergence", "")).strip().lower() in ("true", "1")
    ]

    fig = plt.figure(figsize=(16, 14))
    gs = fig.add_gridspec(4, 3, hspace=0.55, wspace=0.3)

    # ---------------------------------------------------- 1. per-metric grid
    for idx, spec in enumerate(METRICS):
        ax = fig.add_subplot(gs[idx // 3, idx % 3])
        values = [float(r[spec.name]) for r in rows]
        ax.plot(x, values, marker="o", markersize=3, linewidth=1.3, color="#2b6cb0")

        breach_x = [xi for xi, v in zip(x, values) if spec.is_breach(v)]
        breach_y = [v for v in values if spec.is_breach(v)]
        if breach_x:
            ax.scatter(breach_x, breach_y, color="#c53030", zorder=5, s=28,
                       label="SLA breach")

        ax.axhline(spec.threshold, color="#c53030", linestyle="--", linewidth=1,
                   alpha=0.7)
        for di in divergence_idx:
            ax.axvspan(di - 0.4, di + 0.4, color="#f6ad55", alpha=0.25)

        ax.set_title(f"{spec.name}  (SLA {'<=' if spec.direction == 'max' else '>='} "
                     f"{spec.threshold}{spec.unit})", fontsize=10)
        ax.set_xlabel("sample #", fontsize=8)
        ax.set_ylabel(spec.unit or "value", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.25)

    # ---------------------------------------------- 2. health/anomaly panel
    ax2 = fig.add_subplot(gs[3, :])
    health = [float(r["health_score"]) for r in rows]
    anomaly = [float(r["anomaly_score"]) for r in rows]

    ax2.plot(x, health, marker="o", markersize=4, linewidth=2, color="#2f855a",
             label="Health score (gate: ML + SLA + trend + volatility)")
    ax2.plot(x, anomaly, marker="s", markersize=3, linewidth=1.3, color="#805ad5",
             linestyle=":", label="Anomaly score (Isolation Forest only)")
    ax2.axhline(GATE_THRESHOLD, color="#2f855a", linestyle="--", linewidth=1, alpha=0.6,
                label=f"Gate threshold ({GATE_THRESHOLD})")

    for i, r in enumerate(rows, start=1):
        decision = r.get("decision", "")
        naive = r.get("naive_decision", decision)
        color = "#2f855a" if decision == "PROMOTE" else "#c53030"
        ax2.scatter([i], [health[i - 1]], color=color, s=90, zorder=6,
                   edgecolor="black", linewidth=0.6)
        if naive != decision:
            ax2.annotate(
                f"naive:{naive}\ngate:{decision}",
                xy=(i, health[i - 1]), xytext=(i, health[i - 1] - 18),
                fontsize=7, ha="center", color="#9c4221",
                arrowprops=dict(arrowstyle="->", color="#9c4221", lw=0.8),
            )

    for di in divergence_idx:
        ax2.axvspan(di - 0.4, di + 0.4, color="#f6ad55", alpha=0.25)

    ax2.set_ylim(-5, 105)
    ax2.set_xlabel("sample # (hover timestamps: see legend / CSV)", fontsize=9)
    ax2.set_ylabel("score (0-100)", fontsize=9)
    ax2.set_title(
        "Gate decision vs. a naive SLA-only monitor "
        f"({len(divergence_idx)} of {len(rows)} samples diverged — shaded bands)",
        fontsize=11,
    )
    ax2.legend(loc="lower left", fontsize=8)
    ax2.grid(alpha=0.25)

    fig.suptitle(
        title or f"Raptor Deployment Gate — live run ({csv_path.name})",
        fontsize=14, fontweight="bold",
    )

    out = Path(out_path) if out_path else csv_path.with_suffix(".png")
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plot a live gate run as a client-facing dashboard PNG")
    parser.add_argument("--csv", required=True, help="live_results_*.csv from scripts/live_sample_and_score.py")
    parser.add_argument("--out", default=None, help="output PNG path (defaults to <csv>.png)")
    parser.add_argument("--title", default=None, help="custom figure title")
    args = parser.parse_args(argv)

    out = render_dashboard(args.csv, args.out, args.title)
    print(f"Dashboard written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
