"""Concrete pipeline notifiers: GitLab CI, Jenkins, generic webhook, no-op."""

from __future__ import annotations

import requests

from .base import GateResult, NotifyError


class NoopNotifier:
    """Default notifier — does nothing. Keeps integration fully optional."""
    name = "noop"

    def notify(self, result: GateResult) -> None:
        return None


class GitLabNotifier:
    """Triggers a GitLab CI pipeline via the trigger token API.

    Passes the gate result as pipeline variables (variables[KEY]=value), so the
    downstream .gitlab-ci.yml can branch on GATE_DECISION.

    Docs: POST /projects/:id/trigger/pipeline
    """
    name = "gitlab"

    def __init__(self, project_url: str, trigger_token: str, ref: str = "main",
                 timeout_seconds: float = 10.0,
                 session: requests.Session | None = None):
        # project_url e.g. https://gitlab.example.com/api/v4/projects/123
        self.project_url = project_url.rstrip("/")
        self.trigger_token = trigger_token
        self.ref = ref
        self.timeout = timeout_seconds
        self._session = session or requests.Session()

    def notify(self, result: GateResult) -> None:
        url = f"{self.project_url}/trigger/pipeline"
        data = {"token": self.trigger_token, "ref": self.ref}
        for key, value in result.as_variables().items():
            data[f"variables[{key}]"] = value
        try:
            resp = self._session.post(url, data=data, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(f"gitlab trigger failed: {exc}") from exc


class JenkinsNotifier:
    """Triggers a Jenkins job via buildWithParameters.

    Passes the gate result as build parameters. Auth uses a user + API token.

    Docs: POST /job/:name/buildWithParameters
    """
    name = "jenkins"

    def __init__(self, base_url: str, job_name: str, user: str, api_token: str,
                 timeout_seconds: float = 10.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.job_name = job_name
        self.user = user
        self.api_token = api_token
        self.timeout = timeout_seconds
        self._session = session or requests.Session()

    def notify(self, result: GateResult) -> None:
        url = f"{self.base_url}/job/{self.job_name}/buildWithParameters"
        try:
            resp = self._session.post(
                url,
                params=result.as_variables(),
                auth=(self.user, self.api_token),
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(f"jenkins trigger failed: {exc}") from exc


class WebhookNotifier:
    """POSTs the full gate result as JSON to any URL."""
    name = "webhook"

    def __init__(self, url: str, timeout_seconds: float = 10.0,
                 session: requests.Session | None = None):
        self.url = url
        self.timeout = timeout_seconds
        self._session = session or requests.Session()

    def notify(self, result: GateResult) -> None:
        payload = {
            "component": result.component,
            "decision": result.decision,
            "health_score": result.health_score,
            "anomaly_score": result.anomaly_score,
            "exit_code": result.exit_code,
            "violations": result.violations,
        }
        try:
            resp = self._session.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(f"webhook POST failed: {exc}") from exc
