"""Notifier interface and the payload sent to pipeline tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..scoring import Verdict


class NotifyError(RuntimeError):
    """Raised when a notifier fails to reach its pipeline tool.

    The CLI catches this and logs it — a notify failure never flips the gate
    decision (fail-open on notification).
    """


@dataclass(frozen=True)
class GateResult:
    """The gate outcome, flattened for pipeline consumption."""
    component: str
    decision: str          # PROMOTE | ROLLBACK
    health_score: float
    anomaly_score: float
    exit_code: int         # 0 | 1
    violations: list[str]  # human-readable breach details

    @classmethod
    def from_verdict(cls, verdict: Verdict) -> "GateResult":
        return cls(
            component=verdict.component,
            decision=verdict.decision,
            health_score=verdict.health_score,
            anomaly_score=verdict.anomaly_score,
            exit_code=verdict.exit_code,
            violations=[v.detail for v in verdict.violations],
        )

    def as_variables(self) -> dict[str, str]:
        """Flatten to string key/values for pipeline variables/params."""
        return {
            "GATE_COMPONENT": self.component,
            "GATE_DECISION": self.decision,
            "GATE_HEALTH_SCORE": str(self.health_score),
            "GATE_ANOMALY_SCORE": str(self.anomaly_score),
            "GATE_EXIT_CODE": str(self.exit_code),
            "GATE_VIOLATIONS": "; ".join(self.violations),
        }


@runtime_checkable
class Notifier(Protocol):
    name: str

    def notify(self, result: GateResult) -> None:
        """Send the gate result to the pipeline tool.

        Raises NotifyError on failure.
        """
        ...
