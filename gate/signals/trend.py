"""Cross-sample persistent-breach tracking.

WHY THIS MODULE EXISTS
  The Isolation Forest (model.py) and the SLA engine (sla.py) both score a
  canary from a SINGLE, isolated sample. Neither has any memory of previous
  deployments. That is a real blind spot: a metric that keeps crossing its
  SLA threshold release after release -- clearly a sign something is wrong
  with the application -- can still PROMOTE every single time if its own
  point penalty (SLA_BREACH_PENALTY) is not, by itself, enough to push that
  one sample's health score below the gate threshold.

  Example this module is designed to catch: a canary breaches
  `redis_latency` on deployment 3 and deployment 5 out of its last 5
  deployments. Each individual sample might still score, say, 73 or 75 and
  PROMOTE on its own (one breach, otherwise clean). But seeing the SAME
  metric break its SLA twice in a 5-sample window is a pattern, not noise --
  and this module forces ROLLBACK when that pattern is detected, regardless
  of what the health-score arithmetic says for that one sample.

WHAT THIS MODULE DOES NOT DO
  It does not detect a metric that is steadily climbing but has never
  actually crossed its SLA threshold (e.g. memory creeping 40% -> 74% while
  the limit is 75%). That is a genuinely different problem -- slope/trend
  detection on the raw values themselves, not breach-pattern tracking across
  samples that have already crossed a line. This module only ever looks at
  samples that DID breach; see docs/how_the_scoring_actually_works.md for the
  worked example of that separate limitation.

HOW IT WORKS
  BreachTracker keeps a short rolling history (default: the last 5 scored
  samples) of WHICH metrics breached their SLA, per component. If any single
  metric appears as a breach in at least `min_breaches` (default: 2) of
  those recent samples, that is a "persistent breach" -- and the caller
  (HealthScorer, in scoring.py) treats that as a hard override: ROLLBACK,
  full stop, independent of the health score.

PERSISTENCE
  A tracker is only useful if its history survives between separate scoring
  calls -- which, in production, often means separate process invocations
  (each `python -m gate.cli --mode production` run is a fresh process). The
  BreachHistoryStore interface below is pluggable so the same BreachTracker
  logic works whether history is kept in memory (tests, one-off scripts),
  in a local JSON file (CLI / local mode, no infra required), or in Redis
  (production mode / the FastAPI serving app, surviving across both process
  restarts and multiple server replicas sharing the same Redis).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PersistentBreach:
    """One metric that has broken its SLA often enough, recently enough, to
    be treated as a standing problem rather than a one-off blip."""
    metric: str
    breach_count: int
    window_size: int
    detail: str


@runtime_checkable
class BreachHistoryStore(Protocol):
    """Storage backend for a component's recent breach history.

    Each entry in the history is the sorted list of metric names that
    breached their SLA on one sample. Implementations only need to persist
    and return that list of lists; BreachTracker owns all the counting
    logic.
    """

    def load(self, component: str) -> list[list[str]]: ...
    def save(self, component: str, history: list[list[str]]) -> None: ...


class InMemoryBreachHistoryStore:
    """Keeps history only for the lifetime of this Python object.

    Good for: unit tests, one-off scripts that loop within a single process
    (e.g. scripts/live_sample_and_score.py), and the FastAPI serving app
    when no Redis connection is available (same graceful-degradation pattern
    FeatureStore already uses).
    """

    def __init__(self) -> None:
        self._data: dict[str, list[list[str]]] = {}

    def load(self, component: str) -> list[list[str]]:
        return [list(entry) for entry in self._data.get(component, [])]

    def save(self, component: str, history: list[list[str]]) -> None:
        self._data[component] = [list(entry) for entry in history]


class FileBreachHistoryStore:
    """Persists history to local JSON files, one per component.

    Good for: CLI / local mode, where `python -m gate.cli` is a brand-new
    process every run and there is no Redis to fall back on. Not safe for
    concurrent writers to the SAME component -- fine for a single CI/CD
    pipeline invoking the gate sequentially, which is the gate's actual
    usage pattern.

    SCALING NOTE (fixed after load testing): an earlier version of this
    class stored every component's history in one shared JSON file and
    rewrote the WHOLE file on every load()/save() call. That made each call
    cost O(total number of components ever scored), not O(1) -- confirmed
    by testing: ~1500 distinct components pushed per-call latency into the
    tens of milliseconds and climbing. This version stores one small file
    per component instead (component names are filesystem-sanitised), so
    each load()/save() only ever touches that one component's file --
    O(1) per call, independent of how many other components exist. This
    now scales the same way RedisBreachHistoryStore always did.
    """

    def __init__(self, path: str | Path = "data/.breach_history") -> None:
        # `path` names the DIRECTORY that holds one small JSON file per
        # component (not a single file itself -- see the scaling note
        # above). A trailing ".json" is stripped for compatibility with
        # callers still passing the old single-file-style path.
        p = Path(path)
        self.dir = p.with_suffix("") if p.suffix == ".json" else p

    @staticmethod
    def _safe_filename(component: str) -> str:
        """Sanitise a component name into a safe filename, collision-free
        for any input via a short content hash suffix."""
        import re
        import hashlib
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", component)[:80]
        digest = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
        return f"{safe}.{digest}.json"

    def _component_path(self, component: str) -> Path:
        return self.dir / self._safe_filename(component)

    def load(self, component: str) -> list[list[str]]:
        p = self._component_path(component)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, component: str, history: list[list[str]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._component_path(component).write_text(json.dumps(history))


class RedisBreachHistoryStore:
    """Persists history in the same shared Redis instance the FeatureStore
    cache already uses (gate/features/store.py) -- so history survives pod
    restarts and is shared across every replica of the serving app.

    Best-effort, like FeatureStore's own cache: a Redis failure here never
    raises and never blocks scoring -- it just means this one sample doesn't
    get recorded, degrading gracefully rather than failing the gate.
    """

    def __init__(self, redis_client, ttl_seconds: int = 3600) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds

    def _key(self, component: str) -> str:
        return f"raptorgate:breach_history:{component}"

    def load(self, component: str) -> list[list[str]]:
        if self._redis is None:
            return []
        try:
            raw = self._redis.get(self._key(component))
        except Exception:
            return []
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def save(self, component: str, history: list[list[str]]) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(self._key(component), self._ttl, json.dumps(history))
        except Exception:
            pass  # best-effort cache, never fail scoring on it


class BreachTracker:
    """Tracks SLA breaches across successive samples per component and
    flags a metric as a 'persistent breach' once it has broken its SLA in
    at least `min_breaches` of the last `window_size` recorded samples.

    This is the mechanism that lets the gate roll back on a single metric
    that keeps crossing its SLA threshold, even when no individual sample's
    SLA_BREACH_PENALTY deduction would, by itself, push that sample's health
    score below the gate threshold.
    """

    def __init__(self, store: BreachHistoryStore | None = None,
                 window_size: int = 5, min_breaches: int = 2) -> None:
        if min_breaches < 1:
            raise ValueError("min_breaches must be >= 1")
        if window_size < min_breaches:
            raise ValueError("window_size must be >= min_breaches")
        self.store = store or InMemoryBreachHistoryStore()
        self.window_size = window_size
        self.min_breaches = min_breaches

    def record_and_check(self, component: str,
                          breached_metrics: set[str]) -> list[PersistentBreach]:
        """Record this sample's breaches for `component`, then return every
        metric that now qualifies as a persistent breach (empty if none)."""
        history = self.store.load(component)
        history.append(sorted(breached_metrics))
        history = history[-self.window_size:]
        self.store.save(component, history)

        counts: dict[str, int] = {}
        for entry in history:
            for metric in entry:
                counts[metric] = counts.get(metric, 0) + 1

        window = len(history)
        persistent: list[PersistentBreach] = []
        for metric, count in sorted(counts.items()):
            if count >= self.min_breaches:
                persistent.append(PersistentBreach(
                    metric=metric,
                    breach_count=count,
                    window_size=window,
                    detail=(
                        f"{metric} violated its SLA in {count} of the last "
                        f"{window} samples for '{component}' -- treated as a "
                        f"persistent application issue, forcing ROLLBACK "
                        f"regardless of this sample's health score."
                    ),
                ))
        return persistent
