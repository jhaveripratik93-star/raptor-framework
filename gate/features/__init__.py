"""Raptor-compatible feature layer.

Mirrors the concepts from the framework document's Raptor usage — @feature and
@aggregation declarations with time windows — but on maintained libraries
instead of the dormant Raptor LabSDK (whose dependency stack no longer runs on
modern Python).

The design intentionally echoes Raptor:
  - @feature(source=..., keys=...)  declares a served feature
  - @aggregation(function=..., over=...) declares its windowed aggregate
  - a FeatureStore computes the windows, caches in Redis, and serves vectors

This keeps the architecture the document describes (5-minute aggregated feature
windows, Redis caching, a served feature/model API) without betting production
on an abandoned package.
"""

from .registry import (
    AggregationFunction,
    FeatureSpec,
    feature,
    aggregation,
    registered_features,
    clear_registry,
)
from .store import FeatureStore

__all__ = [
    "AggregationFunction",
    "FeatureSpec",
    "feature",
    "aggregation",
    "registered_features",
    "clear_registry",
    "FeatureStore",
]
