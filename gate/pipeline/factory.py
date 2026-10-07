"""Build a notifier from config + environment.

Secrets (tokens) always come from environment variables, never from the config
file. The config only names which env var holds each secret. If the selected
tool's config or secret is missing, we fall back to the no-op notifier and note
why, so a misconfiguration disables notification rather than crashing the gate.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Notifier
from .notifiers import (
    NoopNotifier, GitLabNotifier, JenkinsNotifier, WebhookNotifier,
)


def build_notifier(cfg: dict | None, tool: str | None = None) -> Notifier:
    """Return a Notifier for the selected tool.

    `tool` overrides cfg['tool'] when given (e.g. from a CLI flag). Unknown or
    absent tool -> NoopNotifier.
    """
    cfg = cfg or {}
    tool = (tool or cfg.get("tool") or "noop").lower()

    if tool == "noop":
        return NoopNotifier()

    if tool == "gitlab":
        gl = cfg.get("gitlab", {})
        token = os.environ.get(gl.get("token_env", "GITLAB_TRIGGER_TOKEN"), "")
        project_url = gl.get("project_url", "")
        if not token or not project_url:
            return NoopNotifier()
        return GitLabNotifier(
            project_url=project_url,
            trigger_token=token,
            ref=gl.get("ref", "main"),
        )

    if tool == "jenkins":
        jk = cfg.get("jenkins", {})
        token = os.environ.get(jk.get("token_env", "JENKINS_API_TOKEN"), "")
        user = os.environ.get(jk.get("user_env", "JENKINS_USER"), "")
        base_url = jk.get("base_url", "")
        job = jk.get("job_name", "")
        if not token or not user or not base_url or not job:
            return NoopNotifier()
        return JenkinsNotifier(
            base_url=base_url, job_name=job, user=user, api_token=token,
        )

    if tool == "webhook":
        wh = cfg.get("webhook", {})
        url = wh.get("url") or os.environ.get(wh.get("url_env", "GATE_WEBHOOK_URL"), "")
        if not url:
            return NoopNotifier()
        return WebhookNotifier(url=url)

    return NoopNotifier()


def load_pipeline_config(path: str | Path | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    import yaml
    return yaml.safe_load(p.read_text()) or {}
