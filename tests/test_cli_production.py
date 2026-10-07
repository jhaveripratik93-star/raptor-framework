"""Tests for the production-mode CLI path.

The aggregator is monkeypatched so no real infra is touched — we only verify
that production mode scores a collected sample and fails closed on collection
errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate import cli
from gate.collectors.base import CollectorError

DATA = Path(__file__).resolve().parent.parent / "data"
BASELINE = str(DATA / "v1_baseline.csv")
COLLECTORS_CFG = str(
    Path(__file__).resolve().parent.parent / "gate" / "config" / "collectors.yaml"
)

HEALTHY = {
    "cpu_usage": 46, "memory_usage": 53, "pod_restarts": 0,
    "http_error_rate": 0.2, "p99_latency": 90, "availability": 99.8,
    "redis_latency": 3, "endpoint_latency": 88, "packet_loss": 0.15,
}
DEGRADED = {
    "cpu_usage": 78, "memory_usage": 82, "pod_restarts": 4,
    "http_error_rate": 2.8, "p99_latency": 380, "availability": 97.5,
    "redis_latency": 18, "endpoint_latency": 410, "packet_loss": 1.8,
}


# NOTE: --no-breach-tracking is passed in every test below. Without it, each
# `cli.main()` call writes to the real data/.breach_history.json (see
# gate/signals/trend.py + gate/cli.py's _build_scorer) -- that cross-sample tracking
# behaviour is its own concern, covered in isolation by tests/test_trend.py.
# Disabling it here keeps these CLI-path tests side-effect-free and focused
# purely on the production collection/exit-code contract they're testing.


def test_production_promote(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_production_sample",
                        lambda _p: ("v2", HEALTHY))
    rc = cli.main(["--mode", "production", "--baseline", BASELINE,
                   "--collectors", COLLECTORS_CFG, "--no-breach-tracking"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROMOTE" in out


def test_production_rollback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_production_sample",
                        lambda _p: ("v2-degraded", DEGRADED))
    rc = cli.main(["--mode", "production", "--baseline", BASELINE,
                   "--collectors", COLLECTORS_CFG, "--no-breach-tracking"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ROLLBACK" in out


def test_production_fails_closed_on_collection_error(monkeypatch, capsys):
    def _boom(_p):
        raise CollectorError("prometheus unreachable")

    monkeypatch.setattr(cli, "_get_production_sample", _boom)
    rc = cli.main(["--mode", "production", "--baseline", BASELINE,
                   "--collectors", COLLECTORS_CFG, "--no-breach-tracking"])
    err = capsys.readouterr().err
    # Fail closed: never promote when metrics can't be collected.
    assert rc == 1
    assert "ROLLBACK" in err


def test_local_mode_still_requires_scenario(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--mode", "local", "--baseline", BASELINE])
