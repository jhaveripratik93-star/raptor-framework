# Raptor Deployment Gate — model-serving image (Phase 4)
#
# Builds the trained model into the image, then serves it with uvicorn.
# The v1 baseline is baked in and the model is trained at build time, so the
# running container needs no training step and starts fast.

FROM python:3.10-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATE_MODEL_PATH=/app/model/model.joblib

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code, data, and config.
COPY gate/ ./gate/
COPY data/ ./data/

# Train the model at build time so the image ships a ready artifact.
RUN python -m gate.train --baseline data/v1_baseline.csv --out model/model.joblib

# Non-root user for the runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Serve. gate.serving.app:app loads GATE_MODEL_PATH on startup.
CMD ["uvicorn", "gate.serving.app:app", "--host", "0.0.0.0", "--port", "8080"]
