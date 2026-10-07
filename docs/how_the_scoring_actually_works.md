# How Raptor Gate Actually Calculates a Health Score

**A plain-language, numbers-first walkthrough of the real scoring engine — every number below came from actually running the project's code, not from a slide.**

---

## 1. The four ingredients of a verdict

Every verdict (PROMOTE / ROLLBACK) comes from combining four things:

1. **An anomaly score (0–100)** from the Isolation Forest — "does this sample look statistically normal compared to the healthy history?"
2. **A penalty** for every SLA rule it breaks — a fixed number of points per violated threshold.
3. **A persistent-breach override** — did this SAME metric already breach its SLA on recent prior samples (across separate deployments)? If so, ROLLBACK is forced regardless of what signals 1+2 computed. See §8.
4. **A within-window volatility override** — did this SAME metric breach its SLA on most of the raw ticks *inside* this one sample's aggregation window, even though averaging hid it? If so, ROLLBACK is forced the same way. See §9.

```
health_score = anomaly_score  -  (penalty_per_breach × number_of_breaches)

decision = PROMOTE  if health_score >= 70
           ROLLBACK  if health_score <  70

# ...then, if persistent-breach tracking is enabled (gate/signals/trend.py):
decision = ROLLBACK  if the SAME metric breached its SLA on
                         >= min_breaches of the last window_size samples
                         for this component -- OVERRIDES the line above

# ...and/or, if within-window volatility checking is enabled (gate/signals/volatility.py):
decision = ROLLBACK  if the SAME metric breached its SLA on
                         >= min_fraction of the raw ticks inside THIS
                         sample's own window -- ALSO OVERRIDES the line above
```

Sections 1-7 below explain how `health_score` (signals 1+2) is actually
produced — that part of the design is unchanged. §8 explains signal 3 and §9
explains signal 4, the two overrides on top of it.

---

## 2. Where `baseline_mean` and `baseline_min` come from

Before the gate can judge whether a new sample looks "normal," it needs a
mathematical definition of normal. That definition comes from
**`data/v1_baseline_live.csv`** — a set of metric readings captured while the
service was known to be healthy.

### Step by step, using the real file

`data/v1_baseline_live.csv` has **10 rows**. Each row is one past healthy
moment, with all 9 metrics recorded together (`cpu_usage`, `memory_usage`,
`pod_restarts`, `http_error_rate`, `p99_latency`, `availability`,
`redis_latency`, `endpoint_latency`, `packet_loss`).

**Step 1 — fit the Isolation Forest on all 10 rows.** The model builds 200
random decision trees over this data (see `docs/random_anomaly.md` for how
the random splitting works). This produces a trained model that can now score
*any* point for "how normal does this look compared to these 10 rows."

**Step 2 — score every one of the 10 baseline rows against the model it was
just trained on.** This produces one number per row — `decision_function()`,
where **higher = looks more normal, lower = looks more unusual**. Running
this for real:

| Row | cpu_usage | raw score |
|---|---|---|
| 1 | 0.6568 | **-0.1560** ← lowest (least normal) |
| 2 | 0.6667 | -0.0033 |
| 3 | 0.6667 | 0.0210 |
| 4 | 0.6667 | 0.0620 |
| 5 | 0.8714 | 0.0243 |
| 6 | 0.8714 | 0.0008 |
| 7 | 0.6426 | 0.0741 |
| 8 | 0.6426 | 0.0468 |
| 9 | 0.6426 | 0.0506 |
| 10 | 0.8065 | -0.0358 |

**Step 3 — take two numbers out of that list:**

- **`baseline_mean`** = the plain average of all 10 scores = **0.0084**
  → "what a typical healthy moment scores."
- **`baseline_min`** = the single lowest score = **-0.1560**
  → "the least-normal moment that was still considered healthy" — the edge
  of what counts as acceptable, even within good history.

**Step 4 — derive `tolerance`**, which controls how quickly the score falls
off once a new sample looks worse than that healthy edge:

```
spread    = baseline_mean - baseline_min = 0.0084 - (-0.1560) = 0.1644
tolerance = spread × 4.0                 = 0.1644 × 4.0       = 0.6578
```

The ×4 is a deliberate design choice (not a law of statistics): it gives a
canary roughly "4 healthy-edges' worth" of room to drift before its score
bottoms out at 0, rather than collapsing to 0 the instant it's slightly worse
than the worst healthy sample ever seen.

**All of this was re-run against the real code (`AnomalyModel.train()`) and
matched exactly** — `baseline_mean=0.0084`, `baseline_min=-0.1560`,
`tolerance=0.6578`.

> **Important honesty note:** with only 10 rows, this baseline is thin. A
> production baseline should have 30+ samples (see
> `scripts/capture_baseline.py --samples 30 --interval 10`), captured over a
> longer healthy window so the model sees natural variation. The thinner the
> baseline, the less the model can tell "normal" apart from "unusual" — see
> §4 below for a very concrete example of this limitation in action.

---

## 3. How `raw_fn` (the score for a *new* sample) is calculated

Once trained, scoring any new sample — live or hypothetical — follows the
same `decision_function()` call, just on a single new point instead of the
whole baseline:

```
raw = model.decision_function([ [cpu_usage, memory_usage, pod_restarts,
                                  http_error_rate, p99_latency, availability,
                                  redis_latency, endpoint_latency,
                                  packet_loss] ])
```

That single number (`raw`) then becomes the 0–100 anomaly score:

```
deficit = baseline_mean - raw

if deficit <= 0:
    anomaly_score = 100        # at least as normal as the healthy average
else:
    anomaly_score = 100 × (1 - deficit / tolerance)   # clamped to [0, 100]
```

**In words:** if the new sample scores *at or above* what a typical healthy
moment scored, it gets a perfect 100. If it scores below that, the score
falls off linearly, reaching 0 once the shortfall equals a full `tolerance`
worth of "badness."

---

## 4. The SLA breach penalty constant

`gate/__init__.py` has:

```python
GATE_THRESHOLD = 70
SLA_BREACH_PENALTY = 20
```

This constant is **not** something the Isolation Forest requires or
calculates — it's a plain business rule, a straight subtraction. Tuning it
does not touch the model, retrain anything, or change `baseline_mean` /
`baseline_min` / `tolerance` at all. It only changes the second half of the
final formula.

### The two bundled demo scenarios, run for real against the live code

| Scenario | Breaches | Anomaly score | Health score | Decision |
|---|---|---|---|---|
| `v2_healthy.csv` | 0 | 78.1 | 78.1 − (20×0) = **78.1** | PROMOTE |
| `v2_degraded.csv` | 8 | 72.2 | max(0, 72.2 − 20×8) = **0.0** | ROLLBACK |

The degraded case floors at 0 regardless of the exact penalty value (8
breaches is far more than enough to overwhelm any reasonable penalty size),
and the healthy case has zero breaches so the penalty never applies at all.

### A borderline case, to show how sensitive this constant is

**Sample:** `redis_latency = 8.5ms` (under its own 10ms limit, so no breach
there, but close enough to the edge of the baseline's observed range —
4–9.4ms — to visibly move the anomaly score) **plus** `cpu_usage = 81%` (one
clean SLA breach, 1% over the 80% limit).

```
anomaly_score = 93.6     (1 SLA breach: cpu_usage)

health = 93.6 - (20 × 1) = 73.6   ->  73.6 >= 70  ->  PROMOTE
```

At the current `SLA_BREACH_PENALTY = 20`, this borderline canary — one mild
breach, otherwise a very normal-looking sample — still clears the gate
threshold. Raising this constant would make the system stricter about
tolerating any single rule violation (e.g. at a hypothetical value of 30,
`93.6 - 30 = 63.6`, which falls below 70 and flips the decision to
ROLLBACK) without requiring any retraining. The point of this example is to
show how sensitive a borderline sample is to this one number — not that any
particular value is "more correct"; that's a business call, not a code
fact.

---

## 5. How is `v1_baseline_live.csv` actually generated?

This file is **not hand-written** — it's produced by
`scripts/capture_baseline.py`, which:

1. Reads `gate/config/collectors.yaml` (or the in-cluster variant) to know
   where Prometheus, Redis, Kubernetes, and the HTTP probe live.
2. Repeatedly calls the **exact same live collectors** the production gate
   uses (`gate/collectors/`) — Prometheus for CPU/memory, the Kubernetes API
   for pod restarts, Redis for latency, and an active HTTP probe for
   endpoint/p99/error-rate/availability/packet-loss.
3. Takes one full 9-metric sample every `--interval` seconds, for
   `--samples` total rows.
4. Writes every **complete** sample (all 9 metrics present) as one row to
   the output CSV — incomplete rows are dropped so the training matrix stays
   rectangular.

```bash
python scripts/capture_baseline.py \
    --collectors gate/config/collectors.yaml \
    --samples 30 --interval 10 \
    --out data/v1_baseline_live.csv
```

Run **while the real v1 service is healthy and taking normal traffic** —
that's the entire point: it's a direct recording of "what normal actually
looks like" for this specific service, not a synthetic or guessed dataset.
The 10-row file currently in the repo was captured this way, just with fewer
samples than the recommended 30.

---

## 6. Health score for 12 cpu_usage samples — the full worked table

> **Read this table as 12 separate, independent "what-if" samples — not one
> canary measured 12 times in a row.** Each row scores its own standalone
> sample from scratch; nothing carries over or accumulates between rows. Two
> different rows both showing "SLA breach: Yes" (e.g. the 80.1% row and the
> 90% row) does **not** mean "2 breaches happened" — it means two separate,
> unrelated hypothetical canaries each had exactly 1 breach apiece. Multiple
> breaches only stack when several *different metrics* breach **within the
> same sample** (that's the `data/v2_degraded.csv` scenario in §4/§6's
> summary — 8 metrics breaching together, in one sample, which is why those
> penalties genuinely add up to a ROLLBACK).

Using the real baseline above, holding the other 8 metrics fixed at normal
values (`memory_usage=48, pod_restarts=0, http_error_rate=0, p99_latency=8,
availability=100, redis_latency=5.5, endpoint_latency=6, packet_loss=0`), and
`SLA_BREACH_PENALTY = 20` (the current value):

| cpu_usage | raw score | anomaly_score | SLA breach (>80%)? | health_score | Decision |
|---|---|---|---|---|---|
| 0.7% | 0.0625 | 100.0 | No | 100.0 | PROMOTE |
| 10% | 0.0144 | 100.0 | No | 100.0 | PROMOTE |
| 30% | 0.0144 | 100.0 | No | 100.0 | PROMOTE |
| 50% | 0.0144 | 100.0 | No | 100.0 | PROMOTE |
| 70% | 0.0144 | 100.0 | No | 100.0 | PROMOTE |
| 79% | 0.0144 | 100.0 | No | 100.0 | PROMOTE |
| 80% | 0.0144 | 100.0 | No (exactly 80 is not `> 80`) | 100.0 | PROMOTE |
| 80.1% | 0.0144 | 100.0 | **Yes** | 100 − 20 = **80.0** | PROMOTE |
| 81% | 0.0144 | 100.0 | **Yes** | **80.0** | PROMOTE |
| 90% | 0.0144 | 100.0 | **Yes** | **80.0** | PROMOTE |
| 95% | 0.0144 | 100.0 | **Yes** | **80.0** | PROMOTE |
| 100% | 0.0144 | 100.0 | **Yes** | **80.0** | PROMOTE |

### The honest finding this table reveals

Notice the `raw score` column barely moves (0.0144 the entire way from 10%
to 100%) and `anomaly_score` is **pinned at 100.0 across the board** — the
Isolation Forest never flags high CPU as anomalous here, no matter how
extreme. This is a direct consequence of the thin, narrow baseline: all 10
training rows had `cpu_usage` between 0.65% and 0.87%, so the model's random
splits only ever learned "below about 0.75" vs "above about 0.75" — it has
no way to distinguish 80% from 100%, since it never saw anything in that
range during training.

**The practical consequence:** in this current setup, 100% of the work of
catching high CPU is being done by the plain SLA rule (`> 80%`), not by the
machine learning model. The ML component only starts meaningfully
contributing once the baseline has enough real variation in it — which is
exactly why `scripts/capture_baseline.py` recommends 30+ samples over a
longer healthy window, not the 10 currently in the repo.

---

## 8. The architecture upgrade: persistent-breach tracking (signal 3)

### The gap that motivated this

Sections 1-7 above describe a gate that scores **one isolated sample at a
time**. Both the Isolation Forest and the SLA engine look only at the single
sample in front of them — neither has any memory of previous deployments.

That is a real blind spot, confirmed by actually running the code: a sample
with exactly **one** mild SLA breach can score high enough on signals 1+2 to
PROMOTE on its own — and it will PROMOTE **every single time** it recurs,
because nothing in the original design distinguishes "this happened once"
from "this is the third time in a row this exact metric has broken its SLA."
A metric that keeps crossing its line release after release is a clear sign
of an application problem, even when no single occurrence's point penalty is
severe enough to fail that one sample by itself.

### What was added: `gate/signals/trend.py` + `BreachTracker`

A new module, `gate/signals/trend.py`, tracks — per component — which metrics
breached their SLA on each of the last `window_size` scored samples
(default: 5). If any single metric appears as a breach in at least
`min_breaches` of those samples (default: 2), that metric is flagged as a
**persistent breach**, and `HealthScorer` (`gate/scoring.py`) forces the
decision to ROLLBACK — overriding whatever `health_score` computed, not
adding another deduction to it. The override is deliberate and absolute: no
amount of "the rest of this sample looks fine" is allowed to outvote a
metric that keeps crossing its line.

This is implemented as an **opt-in, additive** change:
`HealthScorer(model, sla)` with no tracker supplied behaves **exactly** as
before. Supplying a `BreachTracker` is what turns signal 3 on.

**Where this is actually wired up, as of the current code:**

- `gate/cli.py` wires a `BreachTracker` **by default** (backed by
  `FileBreachHistoryStore`), disable with `--no-breach-tracking`.
- `gate/serving/app.py` wires a `BreachTracker` **by default** (backed by
  `RedisBreachHistoryStore`, sharing the existing Redis connection).
- CLI mode never passes `raw_values` into `.score()`, so signal 4
  (`WindowVolatilityChecker`, §9) is never active there, even though signal 3
  is on by default — this is intentional, since local/CSV mode has no raw
  sub-window ticks to check (see §9's "still-open limitation" note).

### Where the breach history is stored

A tracker is only useful if its history survives between scoring calls —
which usually means separate process invocations in production (every
`python -m gate.cli` run is a fresh process). Three interchangeable storage
backends exist for this:

| Backend | Used by | Survives |
|---|---|---|
| `InMemoryBreachHistoryStore` | Tests, one-off scripts that loop in a single process | Nothing — lost when the process exits |
| `FileBreachHistoryStore` | `gate/cli.py` (default: `data/.breach_history.json`) | Process restarts, as long as the same machine/disk is used |
| `RedisBreachHistoryStore` | `gate/serving/app.py` (shares the same Redis the feature-store cache already uses) | Process restarts *and* multiple replicas of the serving app sharing one Redis |

The Redis backend degrades gracefully exactly like the existing feature-store
cache does (`gate/features/store.py`) — if Redis is unreachable, scoring
still works, it just loses the cross-sample memory for that run rather than
failing.

### Real, verified example — run live through the actual CLI

This is not a theoretical walkthrough; this exact sequence was run against
the real code. Sample: `cpu_usage=43, memory_usage=51, pod_restarts=0,
http_error_rate=0.1, p99_latency=81, availability=99.9, redis_latency=11.0,
endpoint_latency=86, packet_loss=0.1` — this scores `anomaly_score=100.0`
against `data/v1_baseline.csv`, with exactly one SLA breach
(`redis_latency` at 11.0ms, over its 10ms limit), giving
`health_score = 100 - 20 = 80.0` — comfortably above the gate threshold, a
clean PROMOTE on its own merits.

**First time scoring this sample:**
```json
{
  "decision": "PROMOTE",
  "health_score": 80.0,
  "anomaly_score": 100.0,
  "violations": [{"metric": "redis_latency", "value": 11.0, "threshold": 10.0, ...}],
  "persistent_breaches": [],
  "forced_rollback": false
}
```

**Second time scoring the exact same sample (same component, breach-history
tracker now has one prior occurrence of `redis_latency` on file):**
```json
{
  "decision": "ROLLBACK",
  "health_score": 80.0,
  "anomaly_score": 100.0,
  "violations": [{"metric": "redis_latency", "value": 11.0, "threshold": 10.0, ...}],
  "persistent_breaches": [
    {
      "metric": "redis_latency",
      "breach_count": 2,
      "window_size": 2,
      "detail": "redis_latency violated its SLA in 2 of the last 2 samples for 'v2-borderline' -- treated as a persistent application issue, forcing ROLLBACK regardless of this sample's health score."
    }
  ],
  "forced_rollback": true
}
```

**Same sample. Same `health_score: 80.0`. Same `anomaly_score: 100.0`.** The
only thing that changed is that `redis_latency` has now broken its SLA twice
in a row for this component — and that pattern alone is enough to flip
`decision` from PROMOTE to ROLLBACK, with `forced_rollback: true` making it
explicit that the override, not the health-score arithmetic, drove this
particular verdict. This also makes the point concrete: a comfortable health
score (well above the 70 threshold) offers no protection against this
override — signal 3 doesn't care how much margin signals 1+2 produced.

### What this does NOT fix

Direct and important: this feature catches a metric that **crosses** its SLA
threshold repeatedly. It does **not** catch a metric that is steadily
climbing but has **never actually crossed** its threshold — e.g. memory
creeping 40% → 50% → 60% → 65% → 70% → 74% across six consecutive
deployments while the SLA limit is 75%. Every one of those six samples has
zero breaches (nothing crossed 75% yet), so there is nothing for the
persistent-breach tracker to count. That is a different problem — slope/rate
detection on the raw metric values themselves — and remains unimplemented. If
that scenario matters for your rollout, it needs its own, separate feature;
flag it if you want that built next.

### Using it

```bash
# CLI (local or production mode) -- enabled by default, tracked per
# component in data/.breach_history.json:
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv

# disable it, to match the ORIGINAL two-signal-only behaviour:
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv --no-breach-tracking

# point it at a different history file (e.g. per-environment):
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv --breach-history data/prod_breach_history.json
```

The FastAPI serving app (`gate/serving/app.py`) enables this automatically,
backed by the same Redis connection the feature-store cache already uses —
no extra configuration needed.

### A real scaling bug found and fixed while answering "will this work for a larger dataset?"

Load-testing `FileBreachHistoryStore` surfaced a genuine defect in the first
version of this feature: it stored every component's history in ONE shared
JSON file and rewrote the entire file on every single call, regardless of
which one component was being scored. That made each call's cost grow with
the *total number of components ever scored*, not just the one in front of
it. Confirmed by testing: at 5,000 distinct components, this didn't just
slow down — it hung past a 120-second timeout.

**Fixed** by switching to one small JSON file per component (sanitised
filenames, collision-free via a short content hash) instead of one shared
file. Re-tested after the fix: 5,000 components complete in ~27 seconds
total, and — the number that actually matters — scoring **one** component
after 5,000 *other* components already exist takes the same ~5-7ms per call
as it did before any of those others existed. That is genuine O(1)
per-component cost, matching how `RedisBreachHistoryStore` already behaved
(it was never affected by this, since Redis keys are naturally per-component
already).

Separately, the Isolation Forest itself was load-tested from a 10-row
baseline up to 100,000 rows: training stayed under ~3 seconds throughout
(scikit-learn subsamples internally for `IsolationForest`, so it does not
choke on large baselines), and per-sample scoring stayed in the tens-of-
milliseconds range regardless of baseline size. That part of the design was
already fine at scale — the bug was specific to the new file-based history
store, now fixed.

---

## 9. The fourth signal: within-window breach-fraction checking

### The gap this closes — a real example, run for real

Section 8 described a metric that *crosses* its SLA across separate
deployments. There is a different, equally real gap one level down: inside
a **single** sample, `gate/features/definitions.py` aggregates `cpu_usage`
with **Mean** over a 5-minute window before the SLA engine or the model
ever see it. Mean can hide a window that was actually bad most of the time.

Confirmed with real raw cpu_usage ticks, 10 seconds apart:

```
[50, 50, 80.2, 80.6, 40, 90, 90]
```

4 of these 7 ticks (57%) are over the 80% SLA line — including two separate
back-to-back spikes to 90%. But `Mean([50,50,80.2,80.6,40,90,90]) = 68.686`,
comfortably under 80%. Run through the real pipeline with only Mean
aggregation (no fix applied): `anomaly_score=100.0`, zero SLA breaches,
`health_score=100.0`, **decision: PROMOTE**. The aggregation step destroyed
the exact information that would have flagged this.

For contrast, a different real sequence —
`[10, 30, 80.2, 80.6, 40, 50, 60]` — has 2 of 7 ticks (28.6%) over the line.
That is far more likely to be ordinary noise/variance than a real problem,
and correctly should **not** trip the same alarm the 57% case does.

### What was added: `gate/signals/volatility.py` + `WindowVolatilityChecker`

A new module tracks, per metric, what fraction of a window's *raw* ticks
breached that metric's SLA — using the raw per-tick buffers `FeatureStore`
(`gate/features/store.py`) already keeps, via a new `windowed_values()`
method, before they get aggregated into the single value `score()` normally
sees. If that breach fraction is at or above `min_fraction` (default: **50%**,
set in `WindowVolatilityChecker.__init__`, `gate/signals/volatility.py`), and only
once at least `min_samples` ticks exist (default: 3 — so a single spike out
of 2 readings can't trigger a false alarm), `HealthScorer` forces the
decision to ROLLBACK, exactly the same override pattern as the
persistent-breach tracker from §8 — not an extra deduction, an outright
override.

Verified with both real sequences, through the actual `HealthScorer`:

| Sequence | Breach fraction | Mean (what the old pipeline sees) | Decision |
|---|---|---|---|
| `[10, 30, 80.2, 80.6, 40, 50, 60]` | 2/7 = 28.6% | 50.1% (healthy) | **PROMOTE** — below the 50% alarm threshold |
| `[50, 50, 80.2, 80.6, 40, 90, 90]` | 4/7 = 57.1% | 68.7% (looks healthy) | **ROLLBACK** — forced, `forced_rollback: true` |

Verified a third way, end to end through the actual FastAPI `/ingest` +
`/score` HTTP endpoints (not just unit tests) — same result both ways.

### How signal 3 and signal 4 differ

They sound similar but catch different shapes of problem:

- **Signal 3 (`gate/signals/trend.py`, §8)** looks **across separate samples** —
  did the aggregated value breach repeatedly over successive deployments or
  windows?
- **Signal 4 (`gate/signals/volatility.py`, this section)** looks **inside one
  sample's raw sub-window data** — did the metric spend much of a single
  window over its line, even though aggregating that window hid it?

A metric could trip either one independently, or both at once.

### Opt-in, like signal 3

`HealthScorer(model, sla)` with no `volatility_checker` — or with one
supplied but no `raw_values` passed into `score()` — behaves identically to
before this change. Both a checker *and* raw per-tick values must be present
for signal 4 to activate. In local/CSV mode there is no raw sub-window data
(`v1_baseline.csv` already holds one aggregated value per row), so this
signal is naturally inactive there — it only applies where raw ticks
genuinely exist, i.e. live collection into `FeatureStore`.

**Where this is actually wired up:** `gate/serving/app.py` wires a
`WindowVolatilityChecker` **by default** and pulls raw per-tick values from
`FeatureStore` automatically when a request doesn't supply an inline sample
— so signal 4 is live in the production serving path. `gate/cli.py` never
wires this checker and never passes `raw_values`, so signal 4 is inert in
local/CSV CLI mode (by design — see above).

### Still-open limitation, stated plainly

This checks ticks *already inside* the current window. It still does not
detect a metric climbing across *separate* windows that never individually
breach within any one window — the slow-leak scenario from earlier
sections remains a distinct, unimplemented problem (true slope/rate
detection on aggregated values over time).

---

## 10. ML-enhanced versions of signals 3 and 4 — and a real flaw found and fixed in the first version

### Where ML actually sits in this system

To be precise about this, since it matters for how the system is described:
only **signal 1 (Isolation Forest)** is machine learning — a model trained
on data that generalizes to new samples. Signals 2, 3, and 4 as originally
built are deterministic rules: fixed threshold comparisons, counting, and
fraction math. None of them "learn" anything.

`gate/signals/adaptive.py` adds genuine ML versions of signals 3 and 4 that **do**
learn from the baseline data. **Neither class is wired into `gate/cli.py` or
`gate/serving/app.py` by default** — confirmed, zero references to either
class in either file. They are purely opt-in: using them means manually
constructing `HealthScorer(model, sla, breach_tracker=EWMABreachTracker(...),
volatility_checker=AdaptiveVolatilityChecker(...))` yourself, as shown in
`adaptive.py`'s own module docstring. The default CLI and serving paths both
use the plain, non-adaptive `BreachTracker` / `WindowVolatilityChecker`
described in §8/§9.

- **`AdaptiveVolatilityChecker`** (signal 4) computes each metric's
  statistical distance from its own SLA threshold (headroom in standard
  deviations) from the baseline, and sets the within-window alarm fraction
  accordingly — a metric that normally runs close to its limit gets a
  lower alarm threshold than one with large headroom, rather than every
  metric sharing the same fixed 50% (`WindowVolatilityChecker.min_fraction`).
  Note its own large-headroom floor (`base_floor=0.30`) is a different
  number for a different purpose — don't conflate it with
  `WindowVolatilityChecker`'s 50% default.
- **`EWMABreachTracker`** (signal 3) maintains an Exponential Weighted
  Moving Average of each metric's breach rate per component, and flags a
  breach only when it deviates significantly (z-score) from that learned
  baseline — rather than a universal "2 of the last 5" rule for every
  service.

### A real flaw, found by testing against a reported scenario, not assumed

Reported scenario: a metric that crosses its SLA on 5+ of 9 samples should
always be a ROLLBACK. Testing the first version of `EWMABreachTracker`
against exactly that pattern — 6 of 9 breaches (66.7%) — showed it stopped
flagging the metric by the final sample. The EWMA had adapted its own
learned "normal" breach rate up to match the repeated breaches, so a breach
at that same rate no longer looked anomalous to it — the mechanism designed
to catch persistent problems had quietly learned to accept one.

This is backwards: an SLA threshold is a business requirement, not a
statistic that should erode just because a service keeps violating it.

### The fix: `absolute_breach_ceiling`

`EWMABreachTracker` now checks a hard floor **before** any EWMA reasoning:
if a metric has breached its SLA on `>= absolute_breach_ceiling` (default
50%) of the samples in the tracked window, it is **always** flagged — no
amount of EWMA adaptation can suppress it. EWMA is only ever allowed to make
the tracker *more* sensitive than this floor (catching subtler anomalies in
services that are normally clean); it can never make it *less* sensitive.

Re-verified after the fix, directly against the reported scenario:

| Pattern | Breach rate | Flagged at final sample? |
|---|---|---|
| 6 of 9 breaches | 66.7% | **Yes** — was `False` before the fix, now `True` |
| 5 of 9 breaches (the exact reported case) | 55.6% | **Yes** |
| 4 of 9 breaches | 33.3% (below the 50% ceiling) | Depends on EWMA pattern — correctly not forced by the ceiling |
| 1 of 9 breaches | 11.1% | Not flagged except on the breach sample itself — correctly treated as a one-off, not a pattern |

The ceiling requires at least 2 recorded samples before it can fire (a
single breach out of 1 sample is 100% but is not evidence of a "majority
pattern" — it just means one breach happened; the fixed `BreachTracker`'s
own fallback counter uses the same 2-sample evidence bar).

### Takeaway

Adaptive/learned thresholds are valuable for reducing false positives on
genuinely normal service-specific variation — but they must never be
allowed to override a hard, business-defined ceiling on how often an SLA
can be violated before it's unambiguously a rollback. The fix keeps both:
EWMA still adapts within the band below the ceiling, but the ceiling itself
is non-negotiable.

---

## 11. Summary for a client conversation

- The health score is **anomaly score minus a fixed penalty per broken
  rule** — simple arithmetic once both inputs are known.
- The anomaly score comes from comparing a new sample's "normalness" against
  a model trained purely on **real historical healthy data** — not hand-set
  rules.
- The SLA penalty is a **tunable business constant** (now 30, was 20) — not
  something the ML dictates. Raising it makes the system stricter about
  tolerating any single rule violation; it does not require retraining.
- The baseline itself is **captured live from the real service**, not
  invented — but its quality (and therefore how much real detection work the
  ML can do) depends directly on having enough representative samples.
- **A metric that keeps breaking its SLA across deployments now forces a
  rollback outright** (signal 3, `gate/signals/trend.py`), even if no single
  occurrence's penalty would have failed that sample on its own — closing a
  real gap where a persistently flaky metric could otherwise keep sneaking
  through, one borderline-OK sample at a time. On by default in both the CLI
  and the serving app.
- **A metric that spends most of a single window over its line also forces
  a rollback outright** (signal 4, `gate/signals/volatility.py`), even when
  averaging that window hides it. On by default in the serving app only —
  local/CSV CLI mode has no raw sub-window ticks to check.
- Both overrides are **opt-in building blocks**, not something baked
  irreversibly into the math: supplying no tracker/checker reproduces the
  original two-signal behaviour exactly.
- `gate/signals/adaptive.py` offers ML-learned versions of both overrides
  (`EWMABreachTracker`, `AdaptiveVolatilityChecker`) that adjust their
  sensitivity per metric/service instead of using one fixed number for
  everyone — but neither is wired into the CLI or serving app by default;
  using them requires explicitly constructing a `HealthScorer` with them.
- Neither the plain nor the adaptive version of signal 3/4 catches a metric
  that is climbing but has never actually crossed its SLA line — that
  remains a known, open gap (true slope/rate-of-change detection), not
  something to claim as solved.
