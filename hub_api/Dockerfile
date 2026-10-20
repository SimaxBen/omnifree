# OmniFree hub_api for Google Cloud Run.
# Build from the project root (the image needs core/, plugins/ and config/):
#   docker build -f hub_api/Dockerfile -t omnifree-hub .
# Run locally:
#   docker run -p 8080:8080 -e OMNIFREE_HUB_API_TOKEN=... -e OMNIFREE_VAULT_PASSPHRASE=... omnifree-hub
# Cloud Run injects PORT; secrets come from env vars / Secret Manager mounts or the
# encrypted .env.vault (mount it and set OMNIFREE_VAULT_PATH + OMNIFREE_VAULT_PASSPHRASE).

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    OMNIFREE_ENVIRONMENT=production \
    OMNIFREE_ROOT_DIR=/app \
    OMNIFREE_DATA_DIR=/tmp/omnifree-data \
    OMNIFREE_QUOTA_DB=/tmp/omnifree-data/quota.db

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN grep -v -E '^(pytest|pytest-asyncio|respx)' requirements.txt > requirements.runtime.txt \
    && pip install -r requirements.runtime.txt \
    && rm requirements.runtime.txt

# Optional SDKs for plugins you enable (lazy-imported). Example:
#   docker build --build-arg EXTRA_PIP="asyncpg google-auth" ...
ARG EXTRA_PIP=""
RUN if [ -n "$EXTRA_PIP" ]; then pip install $EXTRA_PIP; fi

COPY core ./core
COPY plugins ./plugins
COPY hub_api ./hub_api
COPY config ./config

RUN useradd --create-home --uid 10001 omnifree \
    && mkdir -p /tmp/omnifree-data \
    && chown -R omnifree /app /tmp/omnifree-data
USER omnifree

EXPOSE 8080

# Shell form so ${PORT} is expanded at runtime (Cloud Run contract).
CMD exec uvicorn hub_api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --no-access-log
