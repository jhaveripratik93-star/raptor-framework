"""FastAPI model-serving app for the deployment gate.

Endpoints:
  GET  /health              liveness/readiness — reports model + redis state
  GET  /features/{comp}     current aggregated feature vector for a component
  POST /score               score a canary -> Verdict (PROMOTE / ROLLBACK)
  POST /ingest              push a raw metric sample into the feature store

The model artifact is loaded once at startup (trained via `python -m gate.train`).
A FeatureStore backs /features and lets /score pull the aggregated vector when a
raw sample isn't supplied inline. Redis is optional — the store degrades to
memory-only if it's not reachable.

Build the app with create_app() so tests can inject a model and fake redis.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from ..metrics import METRIC_NAMES
from ..model import AnomalyModel
from ..scoring import HealthScorer
from ..sla import SlaEngine
from ..features import FeatureStore
from ..signals.trend import BreachTracker, RedisBreachHistoryStore
from ..signals.volatility import WindowVolatilityChecker


class ScoreRequest(BaseModel):
    component: str = Field(..., description="Deployment/component name")
    # Optional inline sample; if omitted, pulled from the feature store.
    sample: dict[str, float] | None = None


class IngestRequest(BaseModel):
    component: str
    sample: dict[str, float]


def _default_model_path() -> str:
    return os.environ.get("GATE_MODEL_PATH", "model/model.joblib")


def _connect_redis():
    """Best-effort Redis connection from env; returns None if unavailable."""
    url = os.environ.get("REDIS_URL")
    host = os.environ.get("REDIS_HOST")
    if not url and not host:
        return None
    try:
        import redis
        if url:
            client = redis.Redis.from_url(url, socket_connect_timeout=2)
        else:
            client = redis.Redis(
                host=host,
                port=int(os.environ.get("REDIS_PORT", 6379)),
                password=os.environ.get("REDIS_PASSWORD") or None,
                socket_connect_timeout=2,
            )
        client.ping()
        return client
    except Exception:
        return None  # degrade to memory-only


def create_app(model: AnomalyModel | None = None,
               store: FeatureStore | None = None,
               sla: SlaEngine | None = None,
               breach_tracker: BreachTracker | None = None,
               volatility_checker: WindowVolatilityChecker | None = None) -> "FastAPI":
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title="Raptor Deployment Gate", version="1.0")

    # --- load model (injected for tests, else from artifact) ---
    model_error: str | None = None
    _model = model
    if _model is None:
        path = _default_model_path()
        try:
            _model = AnomalyModel.load(path)
        except Exception as exc:  # missing/corrupt artifact
            model_error = f"could not load model from {path}: {exc}"

    _redis = None if store is not None else _connect_redis()
    _store = store or FeatureStore(redis_client=_redis)
    _sla = sla or SlaEngine()
    # Cross-sample persistent-breach tracking (gate/signals/trend.py): shares the
    # SAME Redis connection as the feature store, so history survives both
    # process restarts and multiple replicas of this serving app, exactly
    # like the feature-store cache already does. Degrades gracefully to
    # memory-only (per-process) tracking if Redis isn't reachable, instead
    # of failing scoring outright.
    _breach_tracker = breach_tracker or BreachTracker(
        store=RedisBreachHistoryStore(_redis)
    )
    # Within-window breach-fraction checking (gate/signals/volatility.py): catches a
    # metric whose raw ticks breached its SLA on a large fraction of this
    # one window, even though Mean-aggregating those ticks into the single
    # value /score actually scores can hide that entirely. Uses the raw
    # per-tick buffers FeatureStore already keeps -- no extra storage.
    _volatility_checker = volatility_checker or WindowVolatilityChecker()
    _scorer = (
        HealthScorer(_model, _sla, breach_tracker=_breach_tracker,
                    volatility_checker=_volatility_checker)
        if _model is not None else None
    )

    # ------------------------------------------------------------- routes
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok" if _scorer is not None else "degraded",
            "model_loaded": _scorer is not None,
            "model_error": model_error,
            "redis_connected": _redis is not None,
            "features": list(METRIC_NAMES),
        }

    @app.get("/features/{component}")
    def features(component: str) -> dict[str, Any]:
        vector = _store.feature_vector(component)
        return {"component": component, "features": vector}

    @app.post("/ingest")
    def ingest(req: IngestRequest) -> dict[str, Any]:
        _store.ingest(req.component, req.sample)
        return {"ingested": True, "component": req.component}

    @app.post("/score")
    def score(req: ScoreRequest) -> dict[str, Any]:
        if _scorer is None:
            raise HTTPException(status_code=503, detail=model_error or "model not loaded")

        sample = req.sample
        raw_values: dict[str, list[float]] | None = None
        if sample is None:
            # Pull the aggregated feature vector from the store. Also pull
            # the raw within-window ticks that produced it, so the
            # window-volatility signal (gate/signals/volatility.py) can see them --
            # an inline `sample` has no such sub-window history available,
            # so that signal is skipped in that case (unchanged behaviour).
            try:
                sample = _store.feature_vector(req.component)
                raw_values = {
                    m: _store.windowed_values(req.component, m)
                    for m in METRIC_NAMES
                }
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"no sample supplied and none in feature store: {exc}",
                )

        missing = [m for m in METRIC_NAMES if m not in sample]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"sample missing metrics: {missing}"
            )

        verdict = _scorer.score(req.component, sample, raw_values=raw_values)
        return verdict.to_dict()

    return app


# Module-level app for `uvicorn gate.serving.app:app`.
# Guarded so import never crashes if the artifact is missing (health reports it).
try:  # pragma: no cover - exercised via uvicorn, not tests
    app = create_app()
except Exception:  # pragma: no cover
    app = None
