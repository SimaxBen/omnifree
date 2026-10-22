"""Generate a VAPID key pair for the hub's native Web Push channel (see hub_api/webpush.py).

    python -m automation.vapid                  # two lines: VAPID_PUBLIC_KEY=... and VAPID_PRIVATE_KEY=...
    python -m automation.vapid --only public    # just the public value (for piping into the vault)
    python -m automation.vapid --only private   # just the private value

Formats (what the hub and the browsers expect):

* VAPID_PUBLIC_KEY: base64url without padding of the 65-byte uncompressed P-256 point (87 chars, starts with "B");
* VAPID_PRIVATE_KEY: base64url without padding of the raw 32-byte private scalar (43 chars).

Prints to stdout only; nothing is written to disk or to the vault. Generating a new pair invalidates every
existing browser subscription (each device must press "Enable on this device" again).
"""

from __future__ import annotations

import argparse
import sys


def generate() -> tuple[str, str]:
    """(public, private) in the vault formats."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from hub_api.webpush import public_key_of, raw_private_key

    key = ec.generate_private_key(ec.SECP256R1())
    return public_key_of(key), raw_private_key(key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.vapid", description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=("public", "private"), help="print only this value, without the KEY= prefix")
    args = parser.parse_args(argv)
    public, private = generate()
    if args.only == "public":
        print(public)
    elif args.only == "private":
        print(private)
    else:
        print(f"VAPID_PUBLIC_KEY={public}")
        print(f"VAPID_PRIVATE_KEY={private}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
