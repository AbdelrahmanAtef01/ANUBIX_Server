#!/usr/bin/env bash
# Container entrypoint:
#   1. Boot tailscaled (kernel TUN mode REQUIRED — see below).
#   2. Optionally `tailscale up` non-interactively if TAILSCALE_AUTHKEY set.
#   3. Start the FastAPI server.
#
# IMPORTANT — TUN mode is mandatory.
# ─────────────────────────────────
# The service opens an SSH tunnel to the Jetson over the tailnet. paramiko
# (under sshtunnel) makes a direct TCP connection — it does NOT speak
# SOCKS5. Tailscale's userspace-networking mode only routes traffic that
# goes through its SOCKS5 proxy, so the SSH connection times out with
# "Could not establish session to SSH gateway" when we run in userspace.
#
# Therefore we REQUIRE kernel TUN mode, which needs:
#   --cap-add NET_ADMIN  --device /dev/net/tun:/dev/net/tun
# on `docker run`, or the equivalent cap_add/devices blocks in compose.
# If /dev/net/tun is not present we exit non-zero with a clear error so the
# container doesn't masquerade as healthy while every /run silently fails.
#
# If TAILSCALE_AUTHKEY is NOT set the daemon still starts but the tailnet
# is NOT joined — the operator must POST /tailscale/up afterwards to get
# the login URL, click it, and complete auth in a browser.

set -euo pipefail

log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] FATAL: $*" >&2; exit 1; }

# ── TUN check ──────────────────────────────────────────────────────────────
if [[ ! -e /dev/net/tun ]]; then
    cat >&2 <<'EOF'
[entrypoint] FATAL: /dev/net/tun is not mounted into the container.

Tailscale's userspace-networking mode CANNOT carry the SSH tunnel this
service needs (paramiko/sshtunnel make a direct TCP connect; they do not
go through SOCKS5). Re-launch the container with kernel TUN routing:

    docker run -d --name anubix-api-client \
        --network host \
        --cap-add NET_ADMIN \
        --device /dev/net/tun:/dev/net/tun \
        --env-file /home/ubuntu/.env \
        -v anubix-tailscale-state:/var/lib/tailscale \
        abdelrahmanatef01/anubix-api-client:latest

If you're on docker compose, the cap_add/devices blocks in
docker-compose.yml are already uncommented for you — `docker compose up`
should pick them up automatically.

Verify on the host first:
    ls -l /dev/net/tun           # should print c 10 200 ...
    sudo modprobe tun            # if missing (rare on EC2 Ubuntu)
EOF
    die "missing /dev/net/tun"
fi

# ── tailscaled (kernel mode) ───────────────────────────────────────────────
TS_SOCKET=/var/run/tailscale/tailscaled.sock
mkdir -p "$(dirname "$TS_SOCKET")" /var/lib/tailscale

log "TUN device available — starting tailscaled in kernel mode"
tailscaled \
    --state=/var/lib/tailscale/tailscaled.state \
    --socket="$TS_SOCKET" \
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

if [[ ! -S "$TS_SOCKET" ]]; then
    log "tailscaled socket never appeared — see /var/log/tailscaled.log"
    tail -n 50 /var/log/tailscaled.log >&2 || true
    die "tailscaled failed to start"
fi

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
