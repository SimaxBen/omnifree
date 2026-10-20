# OmniFree health monitor worker. Build from the project root:
#   docker build -f deploy/healthmonitor/Dockerfile -t omnifree-healthmonitor .
# Runtime config: HUB_URL, EDGE_URL, PWA_URL, SUPABASE_URL (plain), secrets as env vars
# (HUB_API_TOKEN, HEALTHMONITOR_ALERT_WEBHOOK_URL, ...) or the vault (OMNIFREE_VAULT_PATH +
# OMNIFREE_VAULT_PASSPHRASE). OMNIFREE_MONITOR_TAILNET=1 enables tailnet-only targets.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 OMNIFREE_ROOT_DIR=/app OMNIFREE_DATA_DIR=/tmp/omnifree-data

WORKDIR /app
RUN pip install "httpx>=0.27" "pyyaml>=6.0" "fastapi>=0.115" "uvicorn>=0.30" "pydantic>=2.7" \
    "pydantic-settings>=2.3" "cryptography>=42"

COPY core ./core
COPY healthmonitor ./healthmonitor

RUN useradd --create-home --uid 10001 omnifree && mkdir -p /tmp/omnifree-data && chown -R omnifree /app /tmp/omnifree-data
USER omnifree
EXPOSE 8000
CMD ["python", "-m", "healthmonitor"]
