"""Pipeline integration — notify a CI/CD tool of the gate decision.

The gate's primary contract is its exit code (0 = PROMOTE, 1 = ROLLBACK), which
any pipeline can act on directly. This package adds an OPTIONAL push notification
on top: after scoring, the gate can call GitLab CI, Jenkins, or a generic webhook
to trigger the promote/rollback action, passing the health score and violations.

Integration is fully optional and fail-open: if notifying the pipeline tool
fails, it is logged but never changes the gate's decision or exit code. The
default notifier is a no-op, so nothing fires unless you configure it.
"""

from .base import Notifier, GateResult, NotifyError
from .factory import build_notifier

__all__ = ["Notifier", "GateResult", "NotifyError", "build_notifier"]
