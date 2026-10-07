"""Kubernetes collector — pod_restarts.

Reads the canary pods' container restart counts via the Kubernetes API and
returns the maximum restart count across the matching pods (the worst offender),
which is what the < 3 SLA is meant to catch (crashlooping / OOMKilled pods).
"""

from __future__ import annotations

from .base import CollectorError


class KubernetesCollector:
    name = "kubernetes"
    provides = ("pod_restarts",)

    def __init__(self, namespace: str, label_selector: str,
                 in_cluster: bool = False, api=None):
        self.namespace = namespace
        self.label_selector = label_selector
        self.in_cluster = in_cluster
        self._api = api  # inject a CoreV1Api-like object for tests

    @classmethod
    def from_config(cls, cfg: dict) -> "KubernetesCollector":
        return cls(
            namespace=cfg["namespace"],
            label_selector=cfg.get("pod_label_selector", ""),
            in_cluster=bool(cfg.get("in_cluster", False)),
        )

    def _get_api(self):
        if self._api is not None:
            return self._api
        try:
            from kubernetes import client, config
        except ImportError as exc:  # pragma: no cover
            raise CollectorError("kubernetes package not installed") from exc
        try:
            if self.in_cluster:
                config.load_incluster_config()
            else:
                config.load_kube_config()
        except Exception as exc:
            raise CollectorError(f"could not load kube config: {exc}") from exc
        self._api = client.CoreV1Api()
        return self._api

    def collect(self) -> dict[str, float]:
        api = self._get_api()
        try:
            pods = api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=self.label_selector or None,
            )
        except Exception as exc:  # kubernetes.client.ApiException etc.
            raise CollectorError(f"k8s list pods failed: {exc}") from exc

        items = getattr(pods, "items", []) or []
        if not items:
            raise CollectorError(
                f"no pods matched selector {self.label_selector!r} "
                f"in namespace {self.namespace!r}"
            )

        max_restarts = 0
        for pod in items:
            statuses = (getattr(pod.status, "container_statuses", None) or [])
            for cs in statuses:
                max_restarts = max(max_restarts, int(cs.restart_count or 0))

        return {"pod_restarts": float(max_restarts)}
