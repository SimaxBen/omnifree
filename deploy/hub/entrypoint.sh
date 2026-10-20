#!/bin/sh
# Start tailscaled in userspace mode when TAILSCALE_AUTHKEY is set (use an EPHEMERAL, reusable,
# pre-approved key: Cloud Run instances come and go), then exec the hub command.
set -eu
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  tailscaled --tun=userspace-networking --state=mem: --socket=/var/run/tailscale/tailscaled.sock \
    --outbound-http-proxy-listen=127.0.0.1:1055 --socks5-server=127.0.0.1:1056 >/tmp/tailscaled.log 2>&1 &
  i=0
  until tailscale --socket=/var/run/tailscale/tailscaled.sock up \
        --auth-key="${TAILSCALE_AUTHKEY}" --hostname="${TAILSCALE_HOSTNAME:-omnifree-hub}" --accept-dns=false; do
    i=$((i + 1)); [ "$i" -ge 5 ] && { echo "tailscale up failed; continuing without tailnet" >&2; break; }
    sleep 2
  done
  export HTTP_PROXY="http://127.0.0.1:1055" http_proxy="http://127.0.0.1:1055"
  export NO_PROXY="localhost,127.0.0.1,metadata.google.internal" no_proxy="localhost,127.0.0.1,metadata.google.internal"
  unset TAILSCALE_AUTHKEY
fi
exec "$@"
