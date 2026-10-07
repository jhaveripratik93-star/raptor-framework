"""Command-line runner for the deployment gate.

Two modes, same scoring logic and same exit codes:

  local (default) — read the canary sample from a scenario CSV (no infra):
    python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv

  production — collect the canary sample live from Prometheus / InfluxDB /
  Redis / Kubernetes / network probe (Phase 1 infra in robot-poc):
    python -m gate.cli --mode production \
        --baseline data/v1_baseline.csv \
        --collectors gate/config/collectors.yaml

Exit code is 0 on PROMOTE and 1 on ROLLBACK, so any CI/CD tool can gate on it.
Add --json to emit the full verdict as JSON for pipeline consumption.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dataio import load_baseline, load_scenario
from .model import AnomalyModel
from .scoring import HealthScorer
from .sla import SlaEngine
from .signals.trend import BreachTracker, FileBreachHistoryStore

_HERE = Path(__file__).parent
_DEFAULT_SLA_CONFIG = _HERE / "config" / "production.yaml"
_DEFAULT_COLLECTORS = _HERE / "config" / "collectors.yaml"
_DEFAULT_PIPELINE = _HERE / "config" / "pipeline.yaml"
_DEFAULT_BREACH_HISTORY = Path("data") / ".breach_history"
"""Directory holding one small JSON file per component -- see
FileBreachHistoryStore in gate/signals/trend.py for why this is a directory
of per-component files rather than one shared file."""


def _build_scorer(baseline_path: str, config_path: str | None,
                   breach_history_path: str | None = None,
                   disable_breach_tracking: bool = False) -> HealthScorer:
    baseline = load_baseline(baseline_path)
    model = AnomalyModel.train(baseline)
    sla = SlaEngine.from_yaml(config_path) if config_path else SlaEngine()

    tracker = None
    if not disable_breach_tracking:
        # Each `gate.cli` invocation is a brand-new process (a fresh CI/CD
        # pipeline stage, typically), so cross-sample breach history is
        # persisted to a small local JSON file rather than kept in memory --
        # otherwise every run would start with a blank slate and the
        # persistent-breach feature (gate/signals/trend.py) could never trigger.
        store = FileBreachHistoryStore(breach_history_path or _DEFAULT_BREACH_HISTORY)
        tracker = BreachTracker(store=store)

    return HealthScorer(model, sla, breach_tracker=tracker)


def _get_local_sample(scenario_path: str) -> tuple[str, dict[str, float]]:
    return load_scenario(scenario_path)


def _get_production_sample(collectors_path: str) -> tuple[str, dict[str, float]]:
    """Collect a live canary sample from all configured sources."""
    import yaml
    from .collectors import MetricAggregator

    cfg = yaml.safe_load(Path(collectors_path).read_text())
    component = cfg.get("target", {}).get("workload", "canary")
    aggregator = MetricAggregator.from_config(cfg)

    result = aggregator.collect(require_complete=True)
    return component, result.sample


def _print_human(verdict) -> None:
    banner = "PROMOTE" if verdict.promote else "ROLLBACK"
    line = "=" * 60
    print(line)
    print(f"  Component      : {verdict.component}")
    print(f"  Health score   : {verdict.health_score} / 100  (gate >= {verdict.gate_threshold})")
    print(f"  Anomaly score  : {verdict.anomaly_score} / 100  (Isolation Forest)")
    print(f"  Flagged anomaly: {verdict.is_anomaly}")
    print(f"  SLA breaches   : {len(verdict.violations)}")
    for v in verdict.violations:
        print(f"      - {v.detail}")
    print(line)
    print(f"  GATE DECISION  : {banner}")
    print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raptor deployment gate")
    parser.add_argument("--mode", choices=["local", "production"], default="local",
                        help="local reads a scenario CSV; production collects live metrics")
    parser.add_argument("--baseline", required=True,
                        help="CSV of v1 baseline (healthy) samples to train on")
    parser.add_argument("--scenario", default=None,
                        help="[local mode] CSV with the canary sample to score")
    parser.add_argument("--collectors", default=None,
                        help="[production mode] collectors.yaml with live endpoints")
    parser.add_argument("--config", default=None,
                        help="Optional production.yaml with SLA thresholds")
    parser.add_argument("--json", action="store_true",
                        help="Emit the verdict as JSON instead of text")
    parser.add_argument("--notify", nargs="?", const="__from_config__",
                        default=None,
                        help="Notify a pipeline tool of the decision. Bare "
                             "--notify uses pipeline.yaml's tool; --notify TOOL "
                             "overrides it (gitlab|jenkins|webhook|noop).")
    parser.add_argument("--pipeline-config", default=None,
                        help="Path to pipeline.yaml (defaults to the bundled one)")
    parser.add_argument("--breach-history", default=None,
                        help="Directory holding the cross-sample breach-history "
                             "files, one small JSON file per component "
                             "(defaults to data/.breach_history/). See "
                             "gate/signals/trend.py: if the SAME metric breaches its "
                             "SLA on >= 2 of the last 5 scored samples for a "
                             "component, the gate forces ROLLBACK regardless "
                             "of the health score for that sample.")
    parser.add_argument("--no-breach-tracking", action="store_true",
                        help="Disable cross-sample persistent-breach tracking "
                             "(gate/signals/trend.py) and score each sample purely in "
                             "isolation, matching the gate's original behaviour.")
    args = parser.parse_args(argv)

    # Default to the bundled SLA config if present and none was given.
    if args.config is None and _DEFAULT_SLA_CONFIG.exists():
        args.config = str(_DEFAULT_SLA_CONFIG)

    # Resolve the canary sample according to mode.
    if args.mode == "local":
        if not args.scenario:
            parser.error("--scenario is required in local mode")
        component, sample = _get_local_sample(args.scenario)
    else:  # production
        collectors_path = args.collectors or (
            str(_DEFAULT_COLLECTORS) if _DEFAULT_COLLECTORS.exists() else None
        )
        if not collectors_path:
            parser.error("--collectors is required in production mode")
        try:
            component, sample = _get_production_sample(collectors_path)
        except Exception as exc:
            # Fail closed: if we cannot collect metrics, do NOT promote.
            print(f"ERROR: metric collection failed: {exc}", file=sys.stderr)
            print("GATE DECISION  : ROLLBACK (fail-closed: no reliable metrics)",
                  file=sys.stderr)
            return 1

    scorer = _build_scorer(args.baseline, args.config,
                           breach_history_path=args.breach_history,
                           disable_breach_tracking=args.no_breach_tracking)
    verdict = scorer.score(component, sample)

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        _print_human(verdict)

    # Optional pipeline notification (fail-open: never changes the exit code).
    if args.notify is not None:
        _notify_pipeline(verdict, args.notify, args.pipeline_config)

    return verdict.exit_code


def _notify_pipeline(verdict, notify_arg: str, pipeline_config: str | None) -> None:
    """Fire the configured pipeline notifier. Errors are logged, never fatal."""
    from .pipeline import build_notifier, GateResult, NotifyError
    from .pipeline.factory import load_pipeline_config

    cfg_path = pipeline_config or (
        str(_DEFAULT_PIPELINE) if _DEFAULT_PIPELINE.exists() else None
    )
    cfg = load_pipeline_config(cfg_path)
    # Bare --notify uses the config's tool; --notify TOOL overrides it.
    tool = None if notify_arg == "__from_config__" else notify_arg

    notifier = build_notifier(cfg, tool=tool)
    result = GateResult.from_verdict(verdict)
    try:
        notifier.notify(result)
        if notifier.name != "noop":
            print(f"  Pipeline notified: {notifier.name} "
                  f"({result.decision})", file=sys.stderr)
    except NotifyError as exc:
        # Fail-open: a notification failure must not flip the gate decision.
        print(f"WARNING: pipeline notify ({notifier.name}) failed: {exc}",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
