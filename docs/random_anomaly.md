**Why Isolation Forest is fundamentally "random"**

Isolation Forest doesn't learn boundaries or distributions the way most ML models do. Instead, it builds many random decision trees (an ensemble — n_estimators=200 in this project) where each tree is grown by repeatedly picking a random feature and a random split value within that feature's range, recursively partitioning the data points until each one is isolated in its own leaf.

The key insight: an anomalous point — one that's far from the rest of the data — tends to get isolated in very few random splits, because it sits off on its own and almost any random cut separates it quickly. A normal point sitting in a dense cluster takes many more random splits to isolate, since it keeps landing with other similar points. So average path length to isolation (across all 200 randomly-built trees) becomes the anomaly signal: short average path = anomalous, long average path = normal.

That's the "random" in Isolation Forest — the splits aren't optimized against any objective, they're random by design. Averaging over many random trees is what makes the signal statistically reliable despite each individual tree being arbitrary.

**How this project wires it up (model.py) Training (AnomalyModel.train, lines ~44-67):**

python
##
model = IsolationForest(
    n_estimators=200,
    contamination="auto",
    random_state=42,
)
model.fit(X)
##

X is the matrix of the 9 metrics (cpu_usage, memory_usage, pod_restarts, etc. — the canonical order from 
metrics.py) across every row in the v1 baseline CSV. So the model is trained purely on known-healthy historical samples — it learns what "normal" looks like for this specific service.

random_state=42 makes the randomness reproducible — same baseline data always produces the same trees, same scores, same verdicts. Without pinning this, you'd get a slightly different model (and potentially a different PROMOTE/ROLLBACK call near the threshold) every time you retrained, which would be unacceptable for a production gate.

Calibration (lines ~69-80): the raw sklearn decision_function output is an unbounded, hard-to-interpret number. The code calibrates it against the baseline's own score distribution:

baseline_mean — the average "normalness" score across all healthy baseline samples
baseline_min — the single most anomalous-looking sample within the healthy baseline (the edge of what's still considered normal)
tolerance = (baseline_mean - baseline_min) * 4.0 — defines how far below that healthy edge a canary can drift before its score bottoms out at 0
Scoring a canary (anomaly_health, lines ~82-91):

python

raw = model.decision_function([feature_vector(sample)])[0]
deficit = baseline_mean - raw
health = 100.0 * (1.0 - deficit / tolerance)   # clamped to [0, 100]
A canary that looks at least as normal as the average healthy sample scores 100. The further it drifts below that (in decision-function terms), the faster it falls toward 0, hitting the floor once it's tolerance units worse than the baseline mean.

is_anomaly (lines ~93-96) is a separate, simpler yes/no signal straight from sklearn's predict() — -1 means the model's internal threshold flags it as an outlier, 1 means inlier. This is independent of the 0-100 health score; it's just an extra boolean shown in the verdict output (you've seen is_anomaly: true even on PROMOTE cases in your test runs — that's expected, since sklearn's default anomaly threshold is stricter/different from this project's own calibrated health-score threshold).

**Why this matters for the gate's actual job**

This is the piece that catches what the SLA rule engine (sla.py) can't: combinations of mildly-off metrics. A canary where CPU is a little high, latency is a little high, AND error rate is a little high — none individually breaching their fixed threshold — would sail past the SLA engine untouched. But the Isolation Forest sees that combined point sitting unusually far from the dense cluster of healthy baseline samples (because healthy samples never show all three nudged simultaneously), isolates it in fewer random splits than normal, and docks the health score accordingly. That's the whole value proposition described in the client demo doc's "why this matters" table — catching cross-metric degradation no single threshold would flag.

**Architecture update — a third signal was added (gate/signals/trend.py):** the Isolation Forest and the SLA engine both score one isolated sample at a time; neither has memory of previous deployments. That left a real gap — a metric that keeps breaking its SLA release after release could still PROMOTE every time if that one sample's SLA penalty alone wasn't enough to fail it. `gate/signals/trend.py`'s `BreachTracker` now tracks, per component, which metrics breached their SLA across the last few scored samples; if the SAME metric breaches at least twice in the last five samples, the gate forces ROLLBACK outright, overriding the health score entirely. See `docs/how_the_scoring_actually_works.md` §8 for the full rationale and a real, verified before/after example run through the actual CLI. This is opt-in and fully additive — `HealthScorer` with no tracker supplied behaves exactly as described above, unchanged.


## Metrics Info 

These 9 metrics (defined in 
metrics.py
) were chosen to cover the different ways a deployment can actually go wrong — resource exhaustion, crashes, slow responses, outright errors, and dependency problems. Each one signifies a distinct failure mode that the others wouldn't catch on their own. Here's what each one tells you:

Metric	Source	SLA	What it actually signifies
**cpu_usage**	-
Prometheus	≤ 80%	Is the service computationally overloaded? Sustained high CPU means the new version does more work per request than it should — a performance regression, an inefficient code path, or a traffic spike it can't absorb. Precedes latency problems and eventual crashes if left unchecked.

**memory_usage**-	
Prometheus	≤ 75%	Is the service leaking or over-consuming memory? A canary that creeps toward its memory limit is heading for an OOMKill. Catching this early avoids the pod being forcibly terminated by Kubernetes mid-rollout.

**pod_restarts** -
Kubernetes API	≤ 3	Is the pod crash-looping? This is a direct, unambiguous signal — a healthy pod doesn't restart. Any restarts mean the container is dying and Kubernetes is bringing it back, which is about as serious a signal as you can get.

http_error_rate	Prometheus / live probe	≤ 1%	Is the service actually failing requests? 5xx responses mean the application itself is breaking under real traffic — bugs, unhandled exceptions, downstream failures surfacing as server errors.
p99_latency	InfluxDB / live probe	≤ 200ms	How slow is the worst 1% of requests? p99 (not average) is used deliberately — averages hide tail problems. A service can have a great average latency while 1 in 100 users has a terrible, timeout-inducing experience. This metric specifically protects against that blind spot.
availability	InfluxDB / live probe	≥ 99.5%	Is the service actually reachable and responding at all? This is uptime from the caller's point of view — distinct from error rate, since a service can be "down" (unreachable, timing out) without ever returning an HTTP error code.
redis_latency	Redis	≤ 10ms	Is the shared caching/feature-store layer healthy? Even if the service itself looks fine, a slow Redis means every request that touches the cache is quietly degraded — a dependency problem that wouldn't show up by looking at the service's own metrics alone.
endpoint_latency	Network probe / live probe	≤ 200ms	What's the typical (mean) response time a caller experiences? Complements p99 — this is the everyday-case latency, p99 is the worst-case latency. Together they give both the common experience and the tail risk.
packet_loss	Network probe / live probe	≤ 1%	Is there a network-level problem between the caller and the service — dropped connections, flaky networking, infrastructure issues — as opposed to an application-level problem? This isolates infra/network health from application health.
Why 9 and not fewer: each one is deliberately covering a different layer of what "healthy" means:

Resource health (cpu_usage, memory_usage) — is the container itself under strain?
Process health (pod_restarts) — is the container even staying alive?
Application correctness (http_error_rate) — is the code working?
User-facing performance (p99_latency, endpoint_latency) — is it fast enough, both typically and in the worst case?
Reachability (availability) — can callers even get a response?
Dependency health (redis_latency) — are the things it relies on healthy?
Network health (packet_loss) — is the underlying transport sound?
A deployment can look perfect on any single one of these and still be broken in a way only another metric would reveal — e.g. zero errors and fast p99 latency, but creeping memory usage that will OOM in ten minutes. That's exactly why 
sla.py
 checks all 9 independently (any single breach counts), and why the Isolation Forest (
model.py
) additionally looks at them together — some real failures only show up as an unusual combination (CPU slightly up, latency slightly up, error rate slightly up, each too small individually to breach its own threshold) rather than any one metric crossing its line alone.