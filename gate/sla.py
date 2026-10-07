"""SLA rule engine.

Checks a canary's metric sample against the 9 configured thresholds and returns
the list of breaches. Each breach deducts a fixed penalty from the health score
(handled in scoring.py). Thresholds come from gate/metrics.py by default, or from
a config/production.yaml file when one is provided.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .metrics import METRICS, BY_NAME, MetricSpec


@dataclass(frozen=True)
class Violation:
    metric: str
    value: float
    threshold: float
    direction: str
    detail: str


def load_specs_from_yaml(path: str | Path) -> tuple[MetricSpec, ...]:
    """Load metric specs from a production.yaml file.

    Falls back to the built-in METRICS ordering; only thresholds/units/sources
    are taken from the file so the feature-vector order stays stable.
    """
    import yaml  # local import so the core engine has no hard yaml dependency

    data = yaml.safe_load(Path(path).read_text())
    cfg = data.get("metrics", {})
    specs = []
    for spec in METRICS:  # preserve canonical order
        if spec.name in cfg:
            entry = cfg[spec.name]
            specs.append(
                MetricSpec(
                    name=spec.name,
                    source=str(entry.get("source", spec.source)),
                    unit=str(entry.get("unit", spec.unit)),
                    threshold=float(entry.get("threshold", spec.threshold)),
                    direction=str(entry.get("direction", spec.direction)),
                )
            )
        else:
            specs.append(spec)
    return tuple(specs)


class SlaEngine:
    """Evaluates a metric sample against SLA thresholds."""

    def __init__(self, specs: tuple[MetricSpec, ...] = METRICS):
        self.specs = specs

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SlaEngine":
        return cls(load_specs_from_yaml(path))

    def evaluate(self, sample: dict[str, float]) -> list[Violation]:
        """Return every SLA breach in `sample`, in canonical metric order."""
        violations: list[Violation] = []
        for spec in self.specs:
            if spec.name not in sample:
                raise KeyError(f"sample missing metric {spec.name!r}")
            value = float(sample[spec.name])
            if spec.is_breach(value):
                violations.append(
                    Violation(
                        metric=spec.name,
                        value=value,
                        threshold=spec.threshold,
                        direction=spec.direction,
                        detail=spec.describe_breach(value),
                    )
                )
        return violations

    def check_metric(self, name: str, value: float) -> bool:
        """Return True if a single metric value breaches its SLA."""
        return BY_NAME[name].is_breach(float(value))
