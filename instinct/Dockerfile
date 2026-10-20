# OmniFree Instinct Agent (headless Playwright + FastAPI), multi-arch (runs on Oracle ARM A1).
# Build from the project root:
#   docker build -f instinct/Dockerfile -t omnifree-instinct .
# Run on the Tailscale-only Docker network next to FreeLLMAPI:
#   docker run -d --name instinct --network omnifree -p 100.x.y.z:8090:8090 \
#     -e INSTINCT_API_TOKEN=... -e FREELLMAPI_API_KEY=... -e HUB_API_TOKEN=... \
#     -e INSTINCT_LLM_BASE_URL=http://freellmapi:3001/v1 \
#     -e INSTINCT_HUB_URL=https://<hub>.run.app omnifree-instinct
# The base image tag must match the playwright pip version (browsers are preinstalled).

FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMNIFREE_ROOT_DIR=/app \
    INSTINCT_HOST=0.0.0.0 \
    INSTINCT_PORT=8090

WORKDIR /app

COPY instinct/requirements.txt ./instinct-requirements.txt
RUN pip install -r instinct-requirements.txt

COPY core ./core
COPY instinct ./instinct

# The image ships the non-root user "pwuser" with browsers in /ms-playwright.
RUN chown -R pwuser /app
USER pwuser

EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('INSTINCT_PORT','8090'), timeout=4)"

# Chromium needs a larger /dev/shm or --disable-dev-shm-usage (set in the launcher); add --ipc=host if pages crash.
CMD ["python", "-m", "instinct"]
