# Phase 4 — Deploy the Raptor Gate serving layer to Kubernetes

This deploys the feature store + model-serving API into `robot-poc` — the
framework document's "Raptor feature store serving on Kubernetes (live feature
API)" Next-stage deliverable, built on maintained libraries (FastAPI + the
trained Isolation Forest) rather than the dormant Raptor LabSDK.

## What gets deployed

| Resource | Purpose |
|----------|---------|
| `Deployment/raptor-gate` | 2 replicas of the FastAPI serving app; model baked into the image |
| `Service/raptor-gate` | ClusterIP fronting the pods on port 80 -> container 8080 |

The pods use the Phase-1 Redis (`redis-master.robot-poc`) as the feature-store
cache, reading the password from the existing `redis` Secret.

## Endpoints (served on container port 8080)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | readiness/liveness; reports model + redis state |
| GET | `/features/{component}` | current 5-min aggregated feature vector |
| POST | `/ingest` | push a raw metric sample into the feature store |
| POST | `/score` | score a canary -> PROMOTE / ROLLBACK verdict |

## 1. Build the image

```bash
docker build -t raptor-gate:1.0 .
```

The build trains the model from `data/v1_baseline.csv` and bakes
`model/model.joblib` into the image, so the container starts ready to score.

## 2. Push to a registry the cluster can pull from

```bash
docker tag raptor-gate:1.0 <your-registry>/raptor-gate:1.0
docker push <your-registry>/raptor-gate:1.0
# then set that image ref in deploy/k8s/deployment.yaml
```

## 3. Deploy

```bash
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml

kubectl rollout status deployment/raptor-gate -n robot-poc
kubectl get pods -n robot-poc -l app=raptor-gate
```

## 4. Verify

```bash
kubectl port-forward -n robot-poc svc/raptor-gate 8080:80

# in another terminal:
curl http://localhost:8080/health

curl -X POST http://localhost:8080/score \
  -H 'Content-Type: application/json' \
  -d '{"component":"v2","sample":{"cpu_usage":46,"memory_usage":53,"pod_restarts":0,"http_error_rate":0.2,"p99_latency":90,"availability":99.8,"redis_latency":3,"endpoint_latency":88,"packet_loss":0.15}}'
# -> {"decision":"PROMOTE", ...}
```

## Notes

- **No cluster build environment?** If you can't run `docker build` on the VDI,
  build the image wherever you have Docker (or a CI runner) and push it to a
  registry the cluster can reach. The manifests only need the pushed image ref.
- **Image base is Python 3.10** for broad production support; the local dev venv
  is 3.14, which is fine for development and tests.
- **Retraining:** rebuild the image to retrain from an updated baseline, or mount
  a model artifact and point `GATE_MODEL_PATH` at it to swap models without a
  rebuild.
