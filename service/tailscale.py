"""
Thin wrappers around the `tailscale` CLI for status/up. Used by the
service to expose tailscale state via HTTP.

Container assumption: `tailscaled` is already running in the background
(see entrypoint.sh). We only invoke the `tailscale` client commands.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from typing import Optional


_TS_BIN = shutil.which("tailscale") or "/usr/bin/tailscale"


def tailscale_status() -> dict:
    """Return parsed `tailscale status --json` output, or {ok:false, …}."""
    try:
        r = subprocess.run(
            [_TS_BIN, "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "tailscale binary not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tailscale status timed out"}

    if r.returncode != 0:
        # Common case: daemon up but `tailscale up` not yet run.
        return {
            "ok": False,
            "returncode": r.returncode,
            "stderr": r.stderr.strip()[:500],
            "hint": "tailscaled may be running but not yet authenticated. "
                    "POST /tailscale/up to start auth.",
        }
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "could not parse tailscale status JSON",
                "raw": r.stdout[:500]}

    # Slim down to the interesting fields.
    self_node = data.get("Self", {})
    return {
        "ok":           True,
        "backendState": data.get("BackendState"),
        "self": {
            "hostname":  self_node.get("HostName"),
            "tailscale_ips": self_node.get("TailscaleIPs"),
            "online":    self_node.get("Online"),
            "os":        self_node.get("OS"),
        },
        "magicDNSSuffix": data.get("MagicDNSSuffix"),
        "peers_count":    len(data.get("Peer", {}) or {}),
    }


_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def tailscale_up_with_url(authkey: Optional[str] = None,
                          hostname: str = "anubix-api-client") -> dict:
    """
    Run `tailscale up`. If `authkey` is provided, auths non-interactively
    and returns {ok: true}. Otherwise launches `tailscale up` in the
    background, reads stderr for the login URL, returns it for the operator
    to open in a browser.
    """
    if authkey:
        try:
            r = subprocess.run(
                [_TS_BIN, "up", f"--authkey={authkey}", f"--hostname={hostname}",
                 "--accept-routes"],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "tailscale up with authkey timed out"}
        if r.returncode != 0:
            return {"ok": False, "returncode": r.returncode,
                    "stderr": r.stderr.strip()[:1000]}
        return {"ok": True, "mode": "authkey", "stdout": r.stdout.strip()}

    # Interactive auth — `tailscale up` writes the login URL to stderr then
    # blocks until the user clicks it. We launch it in a background thread,
    # poll stderr for the URL, return the URL, and let auth complete
    # asynchronously.
    proc = subprocess.Popen(
        [_TS_BIN, "up", f"--hostname={hostname}", "--accept-routes"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    captured = {"url": None, "stderr": []}

    def _reader():
        for line in proc.stderr:
            captured["stderr"].append(line)
            m = _URL_RE.search(line)
            if m and captured["url"] is None:
                captured["url"] = m.group(0)

    threading.Thread(target=_reader, daemon=True).start()

    # Poll up to ~10s for the URL to appear.
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and captured["url"] is None and proc.poll() is None:
        time.sleep(0.2)

    if captured["url"]:
        return {
            "ok": True,
            "mode": "interactive",
            "login_url": captured["url"],
            "hint": "Open the URL in a browser, authenticate, then GET /tailscale/status to verify.",
        }

    # Maybe `tailscale up` returned quickly (already authed) — check status.
    if proc.poll() == 0:
        return {"ok": True, "mode": "already-authed", "stderr": "".join(captured["stderr"])[:500]}

    return {
        "ok": False,
        "error": "no login URL seen within 10s; check container logs",
        "stderr": "".join(captured["stderr"])[:1000],
    }
