"""Install the Backblaze B2 bucket CORS rule that browser direct uploads need.

``POST /drive/upload-url`` hands the PWA a B2 upload URL; the browser can only POST to it
when the bucket allows the PWA origin. This calls ``set_cors`` on the ``backblaze_b2``
plugin (secrets from env or the vault, unlocked with ``OMNIFREE_VAULT_PASSPHRASE``)::

    python -m automation.b2_cors --origin https://omnifree.example --origin http://localhost:5173

Only OmniFree's own CORS rule is replaced; other rules and bucket settings are kept.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from core.interfaces import ProviderError
from core.registry import PluginRegistry

__all__ = ["main", "apply_cors"]

DEFAULT_PLUGIN = "backblaze_b2"


def _load_registry() -> PluginRegistry:
    from core.config import get_settings

    settings = get_settings()
    return PluginRegistry.from_file(settings.providers_path, secret_lookup=settings.secret)


async def apply_cors(registry: PluginRegistry, origins: list[str], plugin: str = DEFAULT_PLUGIN) -> dict[str, Any]:
    adapter = await registry.get(plugin, allow_disabled=True)
    set_cors = getattr(adapter, "set_cors", None)
    if set_cors is None:
        raise SystemExit(f"plugin '{plugin}' has no set_cors() (is it the Backblaze B2 adapter?)")
    try:
        return await set_cors(origins)
    finally:
        await registry.close()


def main(argv: Sequence[str] | None = None, *, registry: PluginRegistry | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.b2_cors", description=__doc__.splitlines()[0])
    parser.add_argument("--origin", action="append", required=True, help="allowed browser origin (repeatable)")
    parser.add_argument("--plugin", default=DEFAULT_PLUGIN, help=f"B2 plugin name (default {DEFAULT_PLUGIN})")
    args = parser.parse_args(argv)
    try:
        bucket = asyncio.run(apply_cors(registry or _load_registry(), args.origin, args.plugin))
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rules = [r for r in bucket.get("corsRules") or [] if isinstance(r, dict)]
    print(json.dumps({"bucket": bucket.get("bucketName"), "corsRules": rules}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
