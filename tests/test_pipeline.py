"""Tests for Phase 5 pipeline integration — notifiers, factory, CLI notify."""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.pipeline.base import GateResult, NotifyError
from gate.pipeline.notifiers import (
    NoopNotifier, GitLabNotifier, JenkinsNotifier, WebhookNotifier,
)
from gate.pipeline.factory import build_notifier
from gate.scoring import Verdict
from gate.sla import Violation


def _result(decision="PROMOTE", exit_code=0):
    return GateResult(
        component="v2", decision=decision, health_score=80.0,
        anomaly_score=80.0, exit_code=exit_code,
        violations=["memory_usage=82% violates SLA (must be <= 75%)"],
    )


# ---------------------------------------------------------- payload mapping
def test_gate_result_from_verdict_and_variables():
    v = Verdict(
        component="v2-degraded", decision="ROLLBACK", health_score=0.0,
        anomaly_score=56.7, is_anomaly=True,
        violations=[Violation("memory_usage", 82.0, 75.0, "max", "mem breach")],
    )
    r = GateResult.from_verdict(v)
    assert r.decision == "ROLLBACK"
    assert r.exit_code == 1
    vars_ = r.as_variables()
    assert vars_["GATE_DECISION"] == "ROLLBACK"
    assert vars_["GATE_EXIT_CODE"] == "1"
    assert "mem breach" in vars_["GATE_VIOLATIONS"]


# ------------------------------------------------------------------- capture
class _CaptureSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data=None, params=None, json=None, auth=None, timeout=None):
        self.calls.append({
            "url": url, "data": data, "params": params,
            "json": json, "auth": auth,
        })
        return _OkResp()


class _OkResp:
    def raise_for_status(self):
        pass


class _FailSession:
    def post(self, *a, **k):
        import requests
        raise requests.ConnectionError("boom")


# -------------------------------------------------------------------- GitLab
def test_gitlab_notifier_posts_trigger_with_variables():
    sess = _CaptureSession()
    n = GitLabNotifier("https://gl/api/v4/projects/1", "tok", ref="main",
                       session=sess)
    n.notify(_result(decision="ROLLBACK", exit_code=1))
    call = sess.calls[0]
    assert call["url"].endswith("/trigger/pipeline")
    assert call["data"]["token"] == "tok"
    assert call["data"]["ref"] == "main"
    assert call["data"]["variables[GATE_DECISION]"] == "ROLLBACK"


def test_gitlab_notifier_wraps_errors():
    n = GitLabNotifier("https://gl/api/v4/projects/1", "tok", session=_FailSession())
    with pytest.raises(NotifyError):
        n.notify(_result())


# ------------------------------------------------------------------- Jenkins
def test_jenkins_notifier_build_with_parameters():
    sess = _CaptureSession()
    n = JenkinsNotifier("https://jenkins", "deploy-job", "user", "apitoken",
                        session=sess)
    n.notify(_result())
    call = sess.calls[0]
    assert call["url"].endswith("/job/deploy-job/buildWithParameters")
    assert call["params"]["GATE_DECISION"] == "PROMOTE"
    assert call["auth"] == ("user", "apitoken")


# ------------------------------------------------------------------- Webhook
def test_webhook_notifier_posts_json():
    sess = _CaptureSession()
    n = WebhookNotifier("https://hook", session=sess)
    n.notify(_result(decision="ROLLBACK", exit_code=1))
    call = sess.calls[0]
    assert call["url"] == "https://hook"
    assert call["json"]["decision"] == "ROLLBACK"
    assert call["json"]["exit_code"] == 1


# ------------------------------------------------------------------- factory
def test_factory_defaults_to_noop():
    assert isinstance(build_notifier({}), NoopNotifier)
    assert isinstance(build_notifier({"tool": "unknown"}), NoopNotifier)


def test_factory_gitlab_needs_token_and_url(monkeypatch):
    cfg = {"tool": "gitlab", "gitlab": {"project_url": "https://gl/x",
                                        "token_env": "GL_TOK"}}
    # No env token -> falls back to noop.
    monkeypatch.delenv("GL_TOK", raising=False)
    assert isinstance(build_notifier(cfg), NoopNotifier)
    # With token -> real notifier.
    monkeypatch.setenv("GL_TOK", "secret")
    assert isinstance(build_notifier(cfg), GitLabNotifier)


def test_factory_tool_override():
    cfg = {"tool": "noop"}
    # Even with tool=noop in config, --notify webhook + a URL builds a webhook.
    cfg2 = {"tool": "noop", "webhook": {"url": "https://h"}}
    assert isinstance(build_notifier(cfg2, tool="webhook"), WebhookNotifier)


# --------------------------------------------------------------- CLI notify
DATA = Path(__file__).resolve().parent.parent / "data"


def test_cli_notify_does_not_change_exit_code(monkeypatch, capsys):
    from gate import cli

    fired = {}

    class _Spy:
        name = "webhook"
        def notify(self, result):
            fired["result"] = result

    # _notify_pipeline does `from .pipeline import build_notifier` at call time,
    # so patch the symbol on the gate.pipeline package.
    import gate.pipeline as pipeline_pkg
    monkeypatch.setattr(pipeline_pkg, "build_notifier",
                        lambda cfg, tool=None: _Spy())

    rc = cli.main(["--mode", "local", "--baseline", str(DATA / "v1_baseline.csv"),
                   "--scenario", str(DATA / "v2_degraded.csv"), "--notify", "webhook",
                   "--no-breach-tracking"])
    # Degraded -> ROLLBACK exit 1, and notify fired without altering it.
    assert rc == 1
    assert fired["result"].decision == "ROLLBACK"


def test_cli_notify_fail_open(monkeypatch, capsys):
    from gate import cli
    import gate.pipeline as pipeline_pkg
    from gate.pipeline.base import NotifyError as NE

    class _Boom:
        name = "gitlab"
        def notify(self, result):
            raise NE("unreachable")

    monkeypatch.setattr(pipeline_pkg, "build_notifier",
                        lambda cfg, tool=None: _Boom())

    rc = cli.main(["--mode", "local", "--baseline", str(DATA / "v1_baseline.csv"),
                   "--scenario", str(DATA / "v2_healthy.csv"), "--notify", "gitlab",
                   "--no-breach-tracking"])
    err = capsys.readouterr().err
    # PROMOTE preserved despite the notify failure (fail-open).
    assert rc == 0
    assert "failed" in err.lower()
