"""Generate concurrent HTTP load against a live service, for demo purposes.

Why this exists: gate/config/collectors_incluster_rest-api.yaml sources
cpu_usage from java_lang_operatingsystem_processcpuload -- the TARGET JVM's
own process CPU load via its JMX exporter. That number only moves when the
JVM itself does more work. Stressing the node or a different process (e.g.
stress-ng in a sidecar) will NOT move this metric. Sending the service a
burst of real concurrent requests will -- and it also naturally moves
endpoint_latency / p99_latency / http_error_rate if the service falls
behind, which makes for a more realistic "degraded canary" demo than
spiking one metric in isolation.

SAFETY: this sends real traffic to a real service. Start with low
concurrency and a short duration, watch for errors, and stop (Ctrl+C) if
the service looks like it's struggling more than intended. Do not point
this at anything you don't own or have permission to load-test.

IMPORTANT -- use a REAL route, not "/": this service sits behind an nginx
ingress with `rewrite-target: /cenx$1$2`, meaning its actual application
routes live under /cenx/, not at the bare root. A GET to "/" returns a 404
in ~5ms -- that's the framework's router rejecting an unknown path before
any real handler code, JSON serialization, or backend call ever runs, so
no amount of concurrency against "/" will move cpu_usage or latency at
all (confirmed: 0 effect even at concurrency=1000). GET /cenx/ returns a
real 200 with actual response content and is the correct target.

Usage (run from inside the robot pod, where it has network access to the
in-cluster service DNS name):
    python3.11 scripts/generate_load.py \
        --url http://eric-cenx-rest-api.robot-poc.svc.cluster.local:8080/cenx/ \
        --concurrency 20 --duration 60

Run this in a SEPARATE terminal/exec session, overlapping with (or just
before) scripts/live_sample_and_score.py's capture window, so the capture
actually observes the elevated metrics.
"""
from __future__ import annotations

import argparse
import threading
import time

import requests


def _worker(url: str, timeout: float, stop_at: float, stats: dict, lock: threading.Lock) -> None:
    session = requests.Session()
    while time.time() < stop_at:
        start = time.perf_counter()
        try:
            resp = session.get(url, timeout=timeout)
            ok = resp.status_code < 500
        except requests.RequestException:
            ok = False
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        with lock:
            stats["requests"] += 1
            stats["errors"] += 0 if ok else 1
            stats["total_ms"] += elapsed_ms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate concurrent HTTP load against a service")
    parser.add_argument("--url", required=True,
                         help="full URL to hammer, e.g. "
                              "http://eric-cenx-rest-api.robot-poc.svc.cluster.local:8080/")
    parser.add_argument("--concurrency", type=int, default=10,
                         help="number of concurrent worker threads (default: 10)")
    parser.add_argument("--duration", type=float, default=60.0,
                         help="how long to run, in seconds (default: 60)")
    parser.add_argument("--timeout", type=float, default=5.0,
                         help="per-request timeout, in seconds (default: 5)")
    args = parser.parse_args(argv)

    print(f"Load generator: {args.concurrency} threads hammering {args.url} "
          f"for {args.duration:.0f}s (Ctrl+C to stop early)")
    print("Watch the pod's own metrics (or scripts/live_sample_and_score.py "
          "running in another session) to see the effect.")

    stats = {"requests": 0, "errors": 0, "total_ms": 0.0}
    lock = threading.Lock()
    stop_at = time.time() + args.duration

    threads = [
        threading.Thread(target=_worker, args=(args.url, args.timeout, stop_at, stats, lock), daemon=True)
        for _ in range(max(1, args.concurrency))
    ]
    for t in threads:
        t.start()

    try:
        while time.time() < stop_at:
            time.sleep(5)
            with lock:
                n, err, total_ms = stats["requests"], stats["errors"], stats["total_ms"]
            avg_ms = (total_ms / n) if n else 0.0
            remaining = max(0.0, stop_at - time.time())
            print(f"  ... {n} requests so far, {err} errors, avg latency {avg_ms:.1f}ms "
                  f"({remaining:.0f}s remaining)")
    except KeyboardInterrupt:
        print("\nStopped early by user.")

    for t in threads:
        t.join(timeout=args.timeout + 1)

    with lock:
        n, err, total_ms = stats["requests"], stats["errors"], stats["total_ms"]
    avg_ms = (total_ms / n) if n else 0.0
    print()
    print(f"Done. {n} requests sent, {err} errors ({100 * err / n if n else 0:.1f}%), "
          f"avg latency {avg_ms:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
