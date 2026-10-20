"""OmniFree Personal Instinct Agent.

A small FastAPI service (``instinct.service``) running autonomous browsing tasks
with headless Playwright, driven by a plan -> act loop against the pooled
OpenAI-compatible FreeLLMAPI endpoint. Events (``instinct.task.*``) and base64
JPEG screenshots are pushed to hub_api (``POST /agent/events``) and streamed on
the service's own ``/ws``.

Run: ``python -m instinct`` (or the Docker image in ``instinct/Dockerfile``).
"""
