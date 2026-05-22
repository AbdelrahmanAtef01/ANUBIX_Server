"""
ANUBIX API Client — HTTP Service Wrapper
========================================
A thin FastAPI layer around the existing anubix_api_client.OmniChatRunner.
Imports the client unchanged; only patches the hardcoded PROMPT_PATH so the
container can find the prompt at /app/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt.

Endpoints
---------
GET  /health                       — liveness probe (always 200 if app is up)
GET  /tailscale/status             — current tailscale state
POST /tailscale/up                 — kick off authentication; returns login URL
POST /run                          — accept a mission, return 202 + session_id
                                     immediately; runner executes in background
GET  /run/{session_id}/status      — poll a previously-accepted run

Fire-and-forget
---------------
/run validates the payload, generates a session_id, schedules the runner on a
background thread, and returns 202 right away. The runner's output streams
straight to container stdout (so `docker logs -f` shows the same view the
CLI would print). A small in-memory registry tracks {queued, running, done,
failed} per session_id; this is intentionally process-local (cleared on
restart). For durable results, rely on the Supabase chat-history upload.

Single-runner queue
-------------------
asyncio.Lock around the runner call. Concurrent /run requests are queued
FIFO so the physical robot only ever executes one mission at a time.

Per-request Supabase upload
---------------------------
Each /run can include chat_id + user_id + task_id. If ALL three are present,
this run's anubix text responses are uploaded to public.chats with those
overrides (and session_id = the runner's session-unique agent_name). If any
one is missing, the upload step is skipped entirely.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

# Make the (unchanged) client importable.
SERVICE_DIR = Path(__file__).resolve().parent
APP_ROOT    = SERVICE_DIR.parent          # /app/api_client → contains anubix_api_client.py
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT.parent))  # in case it's mounted differently

import anubix_api_client as ac  # noqa: E402

# Override the hardcoded Windows PROMPT_PATH for container/Linux deployment.
# We do this OUTSIDE the api_client to avoid touching its source.
ac.PROMPT_PATH = Path(
    os.environ.get("ANUBIX_PROMPT_PATH",
                   "/app/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt")
)

# Override Supabase constants from env so secrets stay out of source.
ac.SUPABASE_URL = os.environ.get("SUPABASE_URL", ac.SUPABASE_URL)
ac.SUPABASE_KEY = os.environ.get("SUPABASE_KEY", ac.SUPABASE_KEY)

# Required at startup.
_REQUIRED_ENV = ("OMNI_KEY", "JETSON_PASS")
_missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        f"See service/.env.example."
    )

OMNI_KEY     = os.environ["OMNI_KEY"]
JETSON_IP    = os.environ.get("JETSON_IP",   "100.99.83.67")
JETSON_USER  = os.environ.get("JETSON_USER", "anubix")
JETSON_PASS  = os.environ["JETSON_PASS"]
JETSON_SSH_PORT = int(os.environ.get("JETSON_SSH_PORT", "22"))


# ── Per-request Supabase uploader ─────────────────────────────────────────

class RequestScopedChatUploader:
    """
    A per-request variant of ChatHistoryUploader that takes chat_id /
    user_id / task_id from the request payload instead of module constants.

    chat_id is the PK of public.chats with a uuid_generate_v4() default,
    so we only send it on the FIRST insert of the run (the user-anchor row).
    Subsequent rows omit chat_id and the DB auto-generates a fresh PK; they
    still group with the first row via the shared session_id.
    """

    def __init__(self, url: str, key: str,
                 chat_id: str, user_id: str, task_id: str,
                 sender: str = "anubix"):
        try:
            from supabase import create_client
        except ImportError as exc:
            raise RuntimeError(
                "supabase-py not installed in the container image."
            ) from exc
        self._client    = create_client(url, key)
        self.chat_id    = chat_id
        self.user_id    = user_id
        self.task_id    = task_id
        self.sender     = sender
        self._first_row = True
        self._ok        = 0
        self._fail      = 0

    def upload(self, message: str, session_id: str) -> bool:
        row = {
            "user_id":    self.user_id,
            "sender":     self.sender,
            "message":    message,
            "session_id": session_id,
            "task_id":    self.task_id,
        }
        if self._first_row:
            row["chat_id"] = self.chat_id
            self._first_row = False
        try:
            self._client.table("chats").insert(row).execute()
            self._ok += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self._fail += 1
            print(f"[chats] insert failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return False

    @property
    def stats(self) -> dict:
        return {"ok": self._ok, "failed": self._fail}


# ── Tailscale helpers ─────────────────────────────────────────────────────

from service.tailscale import (  # noqa: E402
    tailscale_status, tailscale_up_with_url,
)


# ── Request / response models ─────────────────────────────────────────────

class Message(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str


class RunRequest(BaseModel):
    # EXACTLY ONE OF prompt | messages is required.
    prompt:   Optional[str]            = Field(default=None, description="Single user prompt.")
    messages: Optional[List[Message]]  = Field(default=None, description="Conversation history; "
                                                                          "last message MUST be role=user.")

    # All three required together to enable Supabase upload.
    chat_id:  Optional[str] = None
    user_id:  Optional[str] = None
    task_id:  Optional[str] = None

    # Runner overrides — defaults match the CLI defaults.
    engine:       str            = "g1-engine"
    temperature:  Optional[float] = 0.1
    share_memory: bool           = False
    max_rounds:   int            = ac.DEFAULT_MAX_ROUNDS


class RunAccepted(BaseModel):
    session_id:          str
    status:              str   # "queued" — by the time the client reads this
    chat_upload_enabled: bool
    message:             str   = "Run accepted. Stream output with `docker logs -f`; " \
                                 "poll GET /run/{session_id}/status for completion."


class RunStatus(BaseModel):
    session_id:          str
    status:              str        # queued | running | done | failed
    final_text:          Optional[str] = None
    error:               Optional[str] = None
    chat_upload_enabled: bool
    chat_upload_stats:   Optional[dict] = None
    started_at:          Optional[float] = None
    finished_at:         Optional[float] = None


class TailscaleUpRequest(BaseModel):
    authkey:  Optional[str] = Field(default=None, description="Optional pre-auth key; falls back to "
                                                              "TAILSCALE_AUTHKEY env var.")
    hostname: str           = Field(default="anubix-api-client")


# ── App + queue ───────────────────────────────────────────────────────────

app = FastAPI(
    title="anubix-api-client",
    description="HTTP wrapper around anubix_api_client.OmniChatRunner. "
                "Fire-and-forget /run with a single-runner FIFO queue so the "
                "physical robot only handles one mission at a time.",
    version="2.0.0",
)

# asyncio.Lock is FIFO — concurrent /run requests are served in arrival order.
_runner_lock = asyncio.Lock()

# In-memory registry of accepted runs (per-process; lost on restart).
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _new_session_id() -> str:
    return f"ANUBIX-session-{uuid.uuid4().hex[:8]}"


def _set_job(session_id: str, **fields: Any) -> None:
    with _jobs_lock:
        if session_id not in _jobs:
            _jobs[session_id] = {}
        _jobs[session_id].update(fields)


def _get_job(session_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(session_id)
        return dict(job) if job is not None else None


@app.get("/health")
async def health() -> dict:
    return {
        "status":      "ok",
        "jetson_ip":   JETSON_IP,
        "jetson_user": JETSON_USER,
        "queue_busy":  _runner_lock.locked(),
        "jobs_in_memory": len(_jobs),
    }


@app.get("/tailscale/status")
async def ts_status() -> dict:
    return tailscale_status()


@app.post("/tailscale/up")
async def ts_up(req: TailscaleUpRequest) -> dict:
    """
    Trigger `tailscale up`. If TAILSCALE_AUTHKEY env or req.authkey is set,
    it auths non-interactively. Otherwise this returns the login URL so the
    operator can open it in a browser.
    """
    authkey = req.authkey or os.environ.get("TAILSCALE_AUTHKEY")
    return tailscale_up_with_url(authkey=authkey, hostname=req.hostname)


@app.post("/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunAccepted)
async def run(req: RunRequest) -> RunAccepted:
    # Validate exactly-one-of prompt | messages.
    if not req.prompt and not req.messages:
        raise HTTPException(400, "Either 'prompt' or 'messages' is required.")
    if req.prompt and req.messages:
        raise HTTPException(400, "Provide either 'prompt' OR 'messages', not both.")
    if req.messages and (not req.messages or req.messages[-1].role != "user"):
        raise HTTPException(400, "'messages' must end with a role='user' entry.")

    # Supabase upload requires all three IDs.
    upload_ids = [req.chat_id, req.user_id, req.task_id]
    upload_enabled = all(upload_ids)
    if any(upload_ids) and not upload_enabled:
        raise HTTPException(
            400,
            "chat_id, user_id, and task_id must all be provided together "
            "(or all omitted to skip Supabase upload).",
        )

    # Pre-assign the session_id so we can return it immediately. The runner
    # will reuse it as its agent_name.
    session_id = _new_session_id()
    _set_job(
        session_id,
        status              = "queued",
        chat_upload_enabled = upload_enabled,
        chat_upload_stats   = None,
        final_text          = None,
        error               = None,
        started_at          = None,
        finished_at         = None,
    )

    # Launch the worker; do NOT await it. The asyncio.Lock inside the worker
    # still serialises concurrent jobs FIFO.
    asyncio.create_task(_run_worker(session_id, req, upload_enabled))

    print(f"[run] accepted session={session_id} "
          f"upload={'on' if upload_enabled else 'off'}", flush=True)

    return RunAccepted(
        session_id          = session_id,
        status              = "queued",
        chat_upload_enabled = upload_enabled,
    )


@app.get("/run/{session_id}/status", response_model=RunStatus)
async def run_status(session_id: str) -> RunStatus:
    job = _get_job(session_id)
    if job is None:
        raise HTTPException(
            404,
            f"No in-memory record of session_id={session_id!r}. "
            "Either the run finished before the process restarted, or the "
            "id is wrong. For durable results, check the Supabase chats "
            "table filtered by this session_id.",
        )
    return RunStatus(session_id=session_id, **job)


async def _run_worker(session_id: str, req: RunRequest, upload_enabled: bool) -> None:
    """Background worker: serialise on the lock, then run synchronously in a thread."""
    async with _runner_lock:
        _set_job(session_id, status="running", started_at=time.time())
        try:
            await asyncio.to_thread(_execute_run, session_id, req, upload_enabled)
        except Exception as exc:  # noqa: BLE001
            # _execute_run already records its own failure; this catches the
            # truly unexpected (e.g. asyncio.to_thread itself).
            print(f"[run] {session_id} worker crashed: "
                  f"{type(exc).__name__}: {exc}", flush=True, file=sys.stderr)
            _set_job(
                session_id,
                status      = "failed",
                error       = f"{type(exc).__name__}: {exc}",
                finished_at = time.time(),
            )


def _execute_run(session_id: str, req: RunRequest, upload_enabled: bool) -> None:
    """
    Run one mission to completion. Writes status into the in-memory job
    registry; runner output goes straight to stdout (visible in docker logs).
    """
    chat_uploader = None
    if upload_enabled:
        chat_uploader = RequestScopedChatUploader(
            url=ac.SUPABASE_URL, key=ac.SUPABASE_KEY,
            chat_id=req.chat_id, user_id=req.user_id, task_id=req.task_id,
        )

    if req.prompt:
        seed_messages = []
        user_prompt   = req.prompt
    else:
        seed_messages = [m.model_dump() for m in (req.messages or [])[:-1]]
        user_prompt   = (req.messages or [])[-1].content

    final_text = ""
    print(f"\n[run] ▶ session={session_id} starting", flush=True)

    try:
        with ac.JetsonTunnel(
            host        = JETSON_IP,
            username    = JETSON_USER,
            password    = JETSON_PASS,
            ssh_port    = JETSON_SSH_PORT,
            local_port  = ac.DEFAULT_LOCAL_PORT,
            remote_port = ac.DEFAULT_REMOTE_PORT,
        ) as tunnel:
            tool_client = ac.JetsonToolClient(
                local_port=tunnel.local_port,
                timeout=ac.DEFAULT_TOOL_TIMEOUT,
            )
            ac.health_check(tool_client)

            runner = ac.OmniChatRunner(
                api_key       = OMNI_KEY,
                tool_client   = tool_client,
                engine        = req.engine,
                max_rounds    = req.max_rounds,
                temperature   = req.temperature,
                share_memory  = req.share_memory,
                chat_uploader = chat_uploader,
                agent_name    = session_id,   # so the session_id we returned == OmniLink agent_name
            )
            if seed_messages:
                runner.messages = list(seed_messages)
            final_text = runner.run(user_prompt)
    except Exception as exc:  # noqa: BLE001
        err_str = f"{type(exc).__name__}: {exc}"
        print(f"[run] ✗ session={session_id} aborted: {err_str}",
              flush=True, file=sys.stderr)
        _set_job(
            session_id,
            status            = "failed",
            error             = err_str,
            final_text        = final_text or None,
            chat_upload_stats = chat_uploader.stats if chat_uploader else None,
            finished_at       = time.time(),
        )
        return

    print(f"[run] ✓ session={session_id} done", flush=True)
    _set_job(
        session_id,
        status            = "done",
        final_text        = final_text,
        chat_upload_stats = chat_uploader.stats if chat_uploader else None,
        finished_at       = time.time(),
    )
