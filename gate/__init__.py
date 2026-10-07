"""Raptor Deployment Gate — canary health scoring and promote/rollback decision.

This package implements the gate logic described in the framework document:
  - 9 monitored metrics with SLA thresholds
  - Isolation Forest anomaly detection trained on the v1 production baseline
  - A 0-100 health score combining anomaly detection with SLA rule checks
  - A PROMOTE (score >= 70) / ROLLBACK (score < 70) decision

Phase 2 runs entirely in local/CSV mode — no cluster or live infra required.
"""

__version__ = "0.1.0"

GATE_THRESHOLD = 70
"""Health score at or above this promotes; below it rolls back."""

SLA_BREACH_PENALTY = 20
"""Points deducted from the health score per SLA breach (per the document)."""
