"""``python -m instinct``: run the Instinct service with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from instinct.config import get_instinct_settings
from instinct.service import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_instinct_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
