#!/usr/bin/env bash
# Container entrypoint:
#   1. Boot tailscaled (TUN mode if /dev/net/tun is mounted, else userspace).
#   2. Optionally `tailscale up` non-interactively if TAILSCALE_AUTHKEY set.
#   3. Start the FastAPI server.
#
# If TAILSCALE_AUTHKEY is NOT set the daemon still starts but the tailnet
# is NOT joined — the operator must POST /tailscale/up afterwards to get
# the login URL, click it, and complete auth in a browser.

set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ── tailscaled ─────────────────────────────────────────────────────────────
TS_SOCKET=/var/run/tailscale/tailscaled.sock
mkdir -p "$(dirname "$TS_SOCKET")" /var/lib/tailscale

if [[ -e /dev/net/tun ]]; then
    log "TUN device available — using kernel networking"
    TS_MODE_ARGS=()
else
    log "no TUN device — falling back to userspace-networking"
    # In userspace mode tailscale also runs a SOCKS5/HTTP proxy. We don't
    # route through those (paramiko/requests do direct TCP) — userspace
    # mode here is mostly a graceful-degrade for hosts without TUN.
    TS_MODE_ARGS=(--tun=userspace-networking
                  --socks5-server=localhost:1055
                  --outbound-http-proxy-listen=localhost:1055)
fi

log "starting tailscaled..."
tailscaled \
    --state=/var/lib/tailscale/tailscaled.state \
    --socket="$TS_SOCKET" \
    "${TS_MODE_ARGS[@]}" \
    >/var/log/tailscaled.log 2>&1 &
TAILSCALED_PID=$!

# Wait for the daemon socket to appear.
for i in $(seq 1 20); do
    if [[ -S "$TS_SOCKET" ]]; then
        log "tailscaled is up (pid=$TAILSCALED_PID)"
        break
    fi
    sleep 0.25
done

# ── optional non-interactive auth ──────────────────────────────────────────
if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
    log "TAILSCALE_AUTHKEY present — joining tailnet non-interactively"
    if tailscale up \
            --authkey="$TAILSCALE_AUTHKEY" \
            --hostname="${TAILSCALE_HOSTNAME:-anubix-api-client}" \
            --accept-routes; then
        log "tailscale up succeeded"
    else
        log "tailscale up FAILED — the service will start, but POST /tailscale/up to retry"
    fi
else
    log "no TAILSCALE_AUTHKEY — daemon is up but tailnet not joined."
    log "POST /tailscale/up to start interactive auth."
fi

# ── server ─────────────────────────────────────────────────────────────────
log "starting uvicorn on 0.0.0.0:${PORT:-6000}"
exec uvicorn service.server:app \
    --host 0.0.0.0 \
    --port "${PORT:-6000}" \
    --log-level "${LOG_LEVEL:-info}"
