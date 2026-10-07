"""Feature declaration decorators and registry.

Usage mirrors Raptor's declarative style:

    @feature(source="prometheus", keys="component")
    @aggregation(function=AggregationFunction.Mean, over="5m")
    def cpu_usage(samples):
        ...

The decorated function's name is the canonical metric name. `@aggregation`
attaches the window + function; `@feature` registers it. The FeatureStore reads
this registry to know how to aggregate each metric's raw samples.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable


class AggregationFunction(str, enum.Enum):
    Mean = "mean"
    Max = "max"
    Min = "min"
    Sum = "sum"
    P99 = "p99"
    Last = "last"


def parse_window(over: str) -> float:
    """Parse a duration like '5m', '30s', '1h' into seconds."""
    over = over.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not over or over[-1] not in units:
        raise ValueError(f"invalid window {over!r}; use e.g. '5m', '30s', '1h'")
    try:
        value = float(over[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid window {over!r}") from exc
    return value * units[over[-1]]


@dataclass
class FeatureSpec:
    name: str
    source: str
    keys: tuple[str, ...]
    function: AggregationFunction
    over: str
    over_seconds: float
    fn: Callable

    def aggregate(self, values: list[float]) -> float:
        if not values:
            raise ValueError(f"no values to aggregate for feature {self.name!r}")
        f = self.function
        if f is AggregationFunction.Mean:
            return sum(values) / len(values)
        if f is AggregationFunction.Max:
            return max(values)
        if f is AggregationFunction.Min:
            return min(values)
        if f is AggregationFunction.Sum:
            return float(sum(values))
        if f is AggregationFunction.Last:
            return values[-1]
        if f is AggregationFunction.P99:
            return _percentile(values, 99.0)
        raise ValueError(f"unsupported aggregation {f!r}")


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (no numpy dependency here)."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


# Module-level registry populated by the decorators.
_REGISTRY: dict[str, FeatureSpec] = {}

# Temporary store for @aggregation metadata before @feature finalises the spec.
_PENDING_AGG: dict[int, dict] = {}


def aggregation(function: AggregationFunction, over: str = "5m"):
    """Attach a windowed aggregation to a feature function."""
    def deco(fn: Callable) -> Callable:
        _PENDING_AGG[id(fn)] = {"function": function, "over": over}
        return fn
    return deco


def feature(source: str, keys: str | tuple[str, ...] = ()):
    """Register a function as a served feature.

    Must be applied ABOVE @aggregation (i.e. outermost), so the aggregation
    metadata is present when the feature is finalised.
    """
    if isinstance(keys, str):
        keys = (keys,)

    def deco(fn: Callable) -> Callable:
        agg = _PENDING_AGG.pop(id(fn), None)
        if agg is None:
            raise ValueError(
                f"feature {fn.__name__!r} needs an @aggregation decorator "
                f"below @feature"
            )
        spec = FeatureSpec(
            name=fn.__name__,
            source=source,
            keys=tuple(keys),
            function=agg["function"],
            over=agg["over"],
            over_seconds=parse_window(agg["over"]),
            fn=fn,
        )
        _REGISTRY[spec.name] = spec
        return fn
    return deco


def registered_features() -> dict[str, FeatureSpec]:
    return dict(_REGISTRY)


def clear_registry() -> None:
    _REGISTRY.clear()
    _PENDING_AGG.clear()
