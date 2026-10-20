"""ASGI entry point: ``uvicorn hub_api.main:app`` (Cloud Run passes the port in ``PORT``)."""

from __future__ import annotations

import os

from core.config import get_settings
from hub_api.app import create_app

app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    port = int(os.environ.get("PORT") or settings.hub_port)
    uvicorn.run(app, host=settings.hub_host, port=port, proxy_headers=True, forwarded_allow_ips="*",
                # Access logs would record ?access_token= (downloads / WebSocket) in plain text.
                access_log=False)


if __name__ == "__main__":
    run()
