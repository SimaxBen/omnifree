"""OmniFree hub_api: FastAPI control plane (LLM pool, agent, drive, DNS, DB, plugins, WebSocket events).

Run locally::

    uvicorn hub_api.main:app --port 8080

or build the Cloud Run image with ``docker build -f hub_api/Dockerfile .`` from the project root.
"""

from hub_api.app import create_app

__all__ = ["create_app"]
