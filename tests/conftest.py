"""Shared pytest fixtures for the gate test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.dataio import load_baseline, load_scenario
from gate.model import AnomalyModel
from gate.scoring import HealthScorer
from gate.sla import SlaEngine

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session")
def baseline():
    return load_baseline(DATA / "v1_baseline.csv")


@pytest.fixture(scope="session")
def model(baseline):
    # Train once for the whole session — training is the slow part.
    return AnomalyModel.train(baseline)


@pytest.fixture(scope="session")
def scorer(model):
    return HealthScorer(model, SlaEngine())


@pytest.fixture(scope="session")
def healthy_sample():
    _, sample = load_scenario(DATA / "v2_healthy.csv")
    return sample


@pytest.fixture(scope="session")
def degraded_sample():
    _, sample = load_scenario(DATA / "v2_degraded.csv")
    return sample


@pytest.fixture
def healthy_baseline_point(baseline):
    """A single known-healthy metric sample, used as the base for per-metric
    SLA tests — start healthy, then push one metric over its threshold."""
    return dict(baseline[0])
