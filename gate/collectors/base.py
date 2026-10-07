"""Collector interface shared by every live data source."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class CollectorError(RuntimeError):
    """Raised when a collector cannot retrieve its metrics.

    The aggregator catches this per-source so one failing source does not abort
    the whole scoring run (it records the failure instead).
    """


@runtime_checkable
class Collector(Protocol):
    """A source of one or more canonical metrics.

    Implementations return a dict of {metric_name: value} covering only the
    metrics they own. `provides` declares which canonical metric names the
    collector is responsible for, so the aggregator can detect gaps.
    """

    name: str
    provides: tuple[str, ...]

    def collect(self) -> dict[str, float]:
        """Query the source and return {metric_name: value}.

        Raises CollectorError on failure.
        """
        ...
