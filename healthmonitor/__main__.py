"""``python -m healthmonitor`` -> serve /healthz + /status on $PORT (default 8000) and run the sweep loop."""

from __future__ import annotations

import logging
import os


def main() -> None:
    import uvicorn

    from healthmonitor.app import create_app

    logging.basicConfig(level=os.environ.get("OMNIFREE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
