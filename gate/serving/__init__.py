"""Model-serving layer for the deployment gate.

A FastAPI app that wraps the trained Isolation Forest + SLA engine and serves
the gate decision as a low-latency API — the document's "deploy the model as a
low-latency scoring API on Kubernetes" role, on maintained libraries.
"""

from .app import create_app

__all__ = ["create_app"]
