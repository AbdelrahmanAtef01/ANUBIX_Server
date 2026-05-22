#!/usr/bin/env python3
"""
anubix_api_client.py
====================
Laptop-side OmniLink /api/chat client for the ANUBIX setup.

What it does (per OmniLink support note "Anubix integration — making tool calls
reach the Jetson over the API"):

  1. Opens an SSH port-forward  laptop:5055 -> jetson:5055  using IP + password.
  2. POSTs your prompt to https://www.omnilink-agents.com/api/chat with the
     EXACT same `systemInstructionRequest` shape that configure_anubix_agent.py
     uses to register the ANUBIX profile (mainTask = full v3.1 prompt, 11
     supervisor_* tools).
  3. For every toolCall the model returns, POSTs { tool, ...arguments } to
     http://127.0.0.1:5055/tool (the Jetson's local tool agent, via the tunnel).
  4. Appends the assistant echo + one role:"tool" message per call and re-POSTs
     /api/chat. Loops until the response has no toolCalls — that's the final
     text answer.
  5. Mirrors the OmniLink website's chat UI in your terminal: agent text,
     each tool call (name + args), each tool result, and the final answer.

Layout (so the prompt-file path stays stable):
  agent_config/
      ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt   <- the canonical prompt
      configure_anubix_agent.py              <- registers the profile server-side
      api_client/
          anubix_api_client.py               <- THIS FILE (loads ../ANUBIX_...txt)

Usage
-----
  $env:OMNI_KEY = "olink_4ekYIgHACfZaGlq6WJOgu59U"
  python anubix_api_client.py `
      --jetson-ip   192.168.1.42 `
      --jetson-user anubix `
      --jetson-pass  *********** `
      --prompt "go check if the plant at x=65 and y=75 has any diseases, ..."

  # Or omit --prompt to enter interactive chat mode (one prompt per line).

Environment variables (CLI flags override these):
  OMNI_KEY        OmniLink API key (required)
  JETSON_IP       Jetson IP/hostname
  JETSON_USER     SSH username on the Jetson  (default: anubix)
  JETSON_PASS     SSH password on the Jetson  (prompted if not set)
  JETSON_SSH_PORT SSH port on the Jetson      (default: 22)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests sshtunnel")
    sys.exit(1)

try:
    from sshtunnel import SSHTunnelForwarder, BaseSSHTunnelForwarderError
except ImportError:
    print("ERROR: 'sshtunnel' not installed. Run: pip install sshtunnel requests")
    sys.exit(1)


# ─── Constants ─────────────────────────────────────────────────────────────

CHAT_URL          = "https://www.omnilink-agents.com/api/chat"
LOCAL_TOOL_HOST   = "127.0.0.1"
DEFAULT_LOCAL_PORT   = 5055
DEFAULT_REMOTE_PORT  = 5055
DEFAULT_SSH_PORT     = 22
DEFAULT_MAX_ROUNDS   = 60
DEFAULT_HTTP_TIMEOUT = 180          # /api/chat (OmniLink server)
DEFAULT_TOOL_TIMEOUT = 180          # /tool POST to Jetson (ROS feedback can take >60s)
HEALTH_PROBE_TIMEOUT = 5            # /health GET — should respond instantly

# This script lives at agent_config/api_client/. The canonical prompt is
# alongside configure_anubix_agent.py, one directory up.
PROMPT_PATH  = Path(r"D:\college_projects\Graduation Project\anubix\anubix_ws\agent_config\ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt")


# ─── ANSI colors (no external deps) ────────────────────────────────────────

class C:
    R       = "\033[0m"
    DIM     = "\033[2m"
    BOLD    = "\033[1m"
    BLUE    = "\033[34m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    RED     = "\033[31m"
    CYAN    = "\033[36m"
    MAG     = "\033[35m"
    GREY    = "\033[90m"


def _enable_ansi_on_windows() -> None:
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


_enable_ansi_on_windows()


def hr(char: str = "─", width: int = 78, color: str = C.GREY) -> None:
    print(f"{color}{char * width}{C.R}")


def _finish_reason(raw: dict) -> str:
    """Pull the engine's finishReason out of /api/chat's `raw` payload.
    g1-engine (Gemini) puts it under raw.candidates[0].finishReason."""
    try:
        return raw["candidates"][0]["finishReason"]
    except (KeyError, IndexError, TypeError):
        return "unknown"


def banner(label: str, color: str = C.CYAN) -> None:
    hr(color=color)
    print(f"{color}{C.BOLD}  {label}{C.R}")
    hr(color=color)


# ─── Tool schema (kept in lockstep with configure_anubix_agent.py) ─────────
#
# Copied verbatim from agent_config/configure_anubix_agent.py so the headless
# request sees identical tools to the registered ANUBIX profile. If you ever
# add/remove a supervisor_* tool, update BOTH files.

ANUBIX_TOOL_DETAILS: list[dict[str, Any]] = [
    {
        "name": "supervisor_robot_id",
        "description": "Set the robot context ID for tracking",
        "parameters": {
            "type": "object",
            "properties": {
                "robot_id": {"type": "string", "description": "UUID or identifier for the robot instance"},
            },
            "required": ["robot_id"],
        },
    },
    {
        "name": "supervisor_task_id",
        "description": "Set the task context ID for tracking",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "UUID or identifier for the specific task"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "supervisor_nav_vision",
        "description": "Set navigation vision mode (true=stop 1m before target for camera, false=drive all the way)",
        "parameters": {
            "type": "object",
            "properties": {
                "vision": {"type": "boolean", "description": "Enable vision mode (stop before target)"},
            },
            "required": ["vision"],
        },
    },
    {
        "name": "supervisor_nav_goal",
        "description": "Navigate robot to specific coordinates",
        "parameters": {
            "type": "object",
            "properties": {
                "x":     {"type": "number", "description": "X coordinate in meters"},
                "y":     {"type": "number", "description": "Y coordinate in meters"},
                "phase": {
                    "type": "string",
                    "description": (
                        "Which navigation step this is. "
                        "'standoff_approach' = Step 2b (vision=true, stop 1m short), "
                        "'final_close' = Step 4b (vision=false, drive all the way). "
                        "REQUIRED so the two calls with identical (x, y) stay structurally distinct."
                    ),
                    "enum": ["standoff_approach", "final_close"],
                },
            },
            "required": ["x", "y", "phase"],
        },
    },
    {
        "name": "supervisor_nav_goal_home",
        "description": "Return robot to home position (0, 0)",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "supervisor_target_camera",
        "description": "Select camera for perception (1=wide-angle, 2=telephoto)",
        "parameters": {
            "type": "object",
            "properties": {
                "camera_number": {
                    "type": "string",
                    "description": "Camera number: '1'=wide-angle, '2'=telephoto",
                    "enum": ["1", "2"],
                },
            },
            "required": ["camera_number"],
        },
    },
    {
        "name": "supervisor_perception_goal",
        "description": "Start perception task",
        "parameters": {
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "Type of perception task",
                    "enum": ["disease", "water_stress", "harvest_status"],
                },
                "phase": {
                    "type": "string",
                    "description": (
                        "Which perception step this is. "
                        "'initial_scan' = Step 3b (camera 1, wide-angle), "
                        "'precision_scan' = Step 6b (camera 2, telephoto). "
                        "REQUIRED so the two calls with identical task_type stay structurally distinct."
                    ),
                    "enum": ["initial_scan", "precision_scan"],
                },
            },
            "required": ["task_type", "phase"],
        },
    },
    {
        "name": "supervisor_arm_nav_goal",
        "description": "Move robotic arm to target or home position",
        "parameters": {
            "type": "object",
            "properties": {
                "signal": {
                    "type": "string",
                    "description": "move=go to target pose, home=return to safe position",
                    "enum": ["move", "home"],
                },
                "phase": {
                    "type": "string",
                    "description": (
                        "Which arm step this is. "
                        "'initial_pose' = Step 5 (first move=move), "
                        "'grip_pose' = Step 7 (second move=move), "
                        "'retract'    = Step 11 (signal=home). "
                        "REQUIRED so the two move calls stay structurally distinct."
                    ),
                    "enum": ["initial_pose", "grip_pose", "retract"],
                },
            },
            "required": ["signal", "phase"],
        },
    },
    {
        "name": "supervisor_grip",
        "description": "Control gripper (open/close)",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "boolean", "description": "true=close gripper, false=open gripper"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "supervisor_spectral_target",
        "description": "Run spectrometer scan on target",
        "parameters": {
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "Type of spectral analysis",
                    "enum": ["disease", "water_stress", "harvest_status"],
                },
                "robot_id": {"type": "string", "description": "Robot ID (optional, uses context if not provided)"},
                "task_id":  {"type": "string", "description": "Task ID (optional, uses context if not provided)"},
            },
            "required": ["task_type"],
        },
    },
    {
        "name": "supervisor_force_stop",
        "description": "Emergency stop - halts all robot operations immediately",
        "parameters": {"type": "object", "properties": {}},
    },
]


ANUBIX_TOOL_NAMES_CSV = ",".join(t["name"] for t in ANUBIX_TOOL_DETAILS)


# Tools whose successful execution legitimately ends the run. If one of these
# was the last dispatched tool and the next response has no tool call, we
# treat the run as complete instead of nudging.
TERMINAL_TOOLS = {"supervisor_nav_goal_home", "supervisor_force_stop"}

GENERIC_NUDGE = (
    "Your last response included narration but did not include the next "
    "tool call required by the standard execution sequence. Every "
    "non-terminal response must emit exactly ONE supervisor_* tool call "
    "alongside the narration. Re-read the last tool result, identify the "
    "current step, and emit the corresponding tool call now."
)

# Cap on consecutive nudges in a single run. Five is enough to recover from
# multiple model stalls (Step 4b nav_goal, Step 6a camera, Step 7 arm, …)
# but won't loop indefinitely if the model is genuinely done.
MAX_NUDGES = 5

# Retries for transient /api/chat failures. OmniLink's primary engine (Gemini)
# occasionally returns 504 PRIMARY_ENGINE_FAILED with a `retryAfterSec` hint.
# We honor that explicitly; otherwise we use exponential backoff.
MAX_API_RETRIES   = 4
RETRY_BACKOFF_CAP = 30.0  # seconds


# ─── Chat-history upload (Supabase `chats` table) ──────────────────────────
#
# Schema:
#   chat_id    uuid PK  (default uuid_generate_v4 — DB-side per-row auto-gen)
#   user_id    uuid     (FK → profiles.id)
#   sender     text     (CHECK: 'user' | 'anubix')
#   message    text
#   created_at timestamptz default now()
#   session_id text default 'Active Terminal'    <- we set this to agent_name
#   task_id    uuid                              <- distinct from prompt task_id
#
# `chat_id` is the primary key and the table has a uuid_generate_v4() default
# — we therefore DO NOT send it on insert (sending the same constant twice
# would violate the PK constraint). It's defined here as a placeholder per
# the request, but only USER_ID, CHATS_TASK_ID, CHATS_SENDER are actually
# sent. session_id is filled in per row from the runner's agent_name.

SUPABASE_URL = "https://bdkutmmrcjckaazzzspe.supabase.co"
SUPABASE_KEY = "sb_publishable_VY6-Jjc6f20Wcbb3Rm8gwg_ZK6CYuh3"

# TODO: replace with real UUIDs from the profiles + tasks rows.
CHAT_ID       = "73f23009-7bb7-40af-93ab-c790a164e72a"  # placeholder — unused; DB auto-gens
CHATS_USER_ID = "c75f1ff9-1d38-4152-9a31-d921fa6904ec"  # FK → profiles.id
CHATS_TASK_ID = "d8898c1d-563d-4dc6-ad54-7424da522e83"  # distinct from prompt task_id
CHATS_SENDER  = "anubix"                                # CHECK constraint allows 'user' | 'anubix'




def load_main_task() -> str:
    """
    Load ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt verbatim. This whole file is the
    `mainTask` — exactly how configure_anubix_agent.py registers the profile.
    """
    if not PROMPT_PATH.exists():
        raise SystemExit(
            f"ERROR: {PROMPT_PATH} not found.\n"
            "  Expected layout:\n"
            "    agent_config/\n"
            "        ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt\n"
            "        api_client/anubix_api_client.py   <-- this file\n"
        )
    return PROMPT_PATH.read_text(encoding="utf-8")


# ─── Pretty-printing helpers (mirror the OmniLink chat UI) ─────────────────

def print_user(prompt: str) -> None:
    banner("USER", color=C.BLUE)
    for line in prompt.splitlines() or [""]:
        print(f"{C.BLUE}  {line}{C.R}")

def print_assistant_text(text: str, round_idx: int) -> None:
    banner(f"ANUBIX  (round {round_idx + 1})", color=C.GREEN)
    if not text.strip():
        print(f"{C.DIM}  (no text — model only emitted tool calls){C.R}")
    else:
        for line in text.splitlines():
            print(f"{C.GREEN}  {line}{C.R}")

def print_tool_call(idx: int, total: int, name: str, args: dict) -> None:
    args_pretty = json.dumps(args, indent=2, ensure_ascii=False)
    args_lines = args_pretty.splitlines() or ["{}"]
    print(f"{C.YELLOW}{C.BOLD}  ▶ tool call {idx+1}/{total}:{C.R} "
          f"{C.YELLOW}{name}{C.R}")
    for line in args_lines:
        print(f"{C.YELLOW}      {line}{C.R}")

def print_tool_result(name: str, result: str, ok: bool) -> None:
    color = C.MAG if ok else C.RED
    label = "tool result" if ok else "tool ERROR"
    print(f"{color}  ◀ {label}  ({name}):{C.R}")
    pretty = result
    try:
        pretty = json.dumps(json.loads(result), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    for line in pretty.splitlines() or [""]:
        print(f"{color}      {line}{C.R}")

def print_final(text: str) -> None:
    banner("FINAL ANSWER", color=C.CYAN)
    if not text.strip():
        print(f"{C.DIM}  (the model returned no text — run is complete){C.R}")
    else:
        for line in text.splitlines():
            print(f"{C.CYAN}{C.BOLD}  {line}{C.R}")
    hr(color=C.CYAN)


# ─── Tool dispatcher (POSTs to the Jetson via the SSH tunnel) ──────────────

class JetsonToolClient:
    def __init__(self, local_port: int, timeout: float = DEFAULT_TOOL_TIMEOUT):
        self.local_port = local_port
        self.base_url = f"http://{LOCAL_TOOL_HOST}:{local_port}"
        self.url      = f"{self.base_url}/tool"
        self.timeout  = timeout

    def health(self) -> tuple[bool, str]:
        """GET /health on the Jetson master node. Returns (ok, message).

        ros_master_node returns {"status": "ok", "node": "anubix_master"} so we
        can prove the right server is on the other end of the tunnel — not
        just *some* TCP listener (e.g. another ssh -L, a stale process)."""
        url = f"{self.base_url}/health"
        try:
            r = requests.get(
                url,
                timeout=HEALTH_PROBE_TIMEOUT,
                headers={"Connection": "close"},
            )
        except requests.RequestException as exc:
            return False, (
                f"health probe failed: {exc}\n"
                f"  The SSH tunnel is up, but {url} did not respond. "
                f"Either the ros_master_node isn't running on the Jetson, or "
                f"something else is bound to port {self.local_port} on the Jetson "
                f"side. From an ssh session into the Jetson, verify with:\n"
                f"    curl -sv http://127.0.0.1:{DEFAULT_REMOTE_PORT}/health\n"
                f"    ros2 run anubix_master master_node    # if it's not running"
            )
        if not r.ok:
            return False, f"health probe returned HTTP {r.status_code}: {r.text[:200]}"
        try:
            body = r.json()
        except ValueError:
            return False, f"health probe returned non-JSON body: {r.text[:200]}"
        node = body.get("node")
        if node != "anubix_master":
            return False, (
                f"health probe returned unexpected body: {body}\n"
                f"  Expected node='anubix_master'. Something else may be bound "
                f"to port {self.local_port}."
            )
        return True, f"master node alive ({body})"

    def dispatch(self, name: str, args: dict) -> tuple[bool, str]:
        """POST one tool call to the Jetson. Returns (ok, stringified_result)."""
        payload = {"tool": name, **(args or {})}
        try:
            # Connection: close so each tool dispatch gets a fresh SSH channel.
            # Persistent connections through paramiko's port-forward can hang
            # if the remote handler closes from its side.
            r = requests.post(
                self.url,
                json=payload,
                timeout=self.timeout,
                headers={"Connection": "close"},
            )
        except requests.RequestException as exc:
            return False, json.dumps({
                "error":  "tool_endpoint_unreachable",
                "url":    self.url,
                "detail": str(exc),
                "hint":   (
                    "TCP connection through the SSH tunnel reached the Jetson, "
                    "but the HTTP request didn't return. Confirm the master "
                    f"node is running: `curl http://127.0.0.1:{DEFAULT_REMOTE_PORT}/health` "
                    "from inside an ssh session into the Jetson."
                ),
            })

        raw = r.text
        try:
            body = r.json()
        except ValueError:
            body = None

        if not r.ok:
            return False, json.dumps({
                "error": "tool_endpoint_http_error",
                "status_code": r.status_code,
                "body": body if body is not None else raw,
            })

        # Jetson agent returns { status, tool, result }. Feed `result` back to
        # the model (stringified, per the tool message contract).
        if isinstance(body, dict) and "result" in body:
            result = body["result"]
        else:
            result = body if body is not None else raw

        if isinstance(result, str):
            return True, result
        return True, json.dumps(result, ensure_ascii=False)


# ─── Chat-history uploader (Supabase) ──────────────────────────────────────

class ChatHistoryUploader:
    """
    Inserts anubix text responses into public.chats. Mirrors the pattern used
    by anubix_supabase.SupabaseUploader — sync client, log + continue on
    failure (chat-history is not load-bearing for the run).
    """

    def __init__(self, url: str, key: str):
        try:
            from supabase import create_client
        except ImportError:
            raise RuntimeError(
                "supabase-py not installed. "
                "pip install supabase  (or pass --no-chat-upload)"
            )
        self._client = create_client(url, key)
        self._ok_count   = 0
        self._fail_count = 0

    def upload(self, message: str, session_id: str) -> bool:
        """Insert one anubix row. Returns True on success."""
        row = {
            "user_id":    CHATS_USER_ID,
            "sender":     CHATS_SENDER,
            "message":    message,
            "session_id": session_id,
            "task_id":    CHATS_TASK_ID,
        }
        try:
            self._client.table("chats").insert(row).execute()
            self._ok_count += 1
            return True
        except Exception as exc:
            self._fail_count += 1
            print(f"{C.YELLOW}[chats] insert failed ({type(exc).__name__}: {exc}). "
                  f"Continuing run; chat-history is not blocking.{C.R}")
            return False

    @property
    def stats(self) -> str:
        return f"chats: ok={self._ok_count} failed={self._fail_count}"


# ─── /api/chat client + dispatch loop ──────────────────────────────────────

class OmniChatRunner:
    def __init__(
        self,
        api_key:      str,
        tool_client:  JetsonToolClient,
        engine:       str           = "g1-engine",
        max_rounds:   int           = DEFAULT_MAX_ROUNDS,
        http_timeout: float         = DEFAULT_HTTP_TIMEOUT,
        agent_name:   Optional[str] = None,
        agent_persona: str          = "Professional",
        temperature:  Optional[float] = 0.1,
        share_memory: bool          = False,
        chat_uploader: Optional[ChatHistoryUploader] = None,
    ):
        self.api_key      = api_key
        self.tool_client  = tool_client
        self.engine       = engine
        self.max_rounds   = max_rounds
        self.http_timeout = http_timeout
        self.temperature  = temperature
        self.agent_persona = agent_persona
        self.chat_uploader = chat_uploader

        # IMPORTANT: agentName scopes persistent memory on the OmniLink server.
        # Re-using "ANUBIX" makes the server CONCATENATE prior conversation
        # memory with our `messages` array, doubling history every round.
        # By default we use a session-unique name to bypass memory entirely
        # (messages array is then the only context). `--share-memory` opts
        # back into the shared-memory pattern the website uses.
        if share_memory or agent_name:
            self.agent_name = agent_name or "ANUBIX"
        else:
            self.agent_name = f"ANUBIX-session-{uuid.uuid4().hex[:8]}"

        # Whole file → mainTask. This mirrors configure_anubix_agent.py exactly.
        # Everything about the agent's text/tool behavior lives in the prompt
        # file; we do NOT layer per-request overrides on top.
        self.main_task = load_main_task()

        self.system_instruction_request: dict[str, Any] = {
            "mainTask":              self.main_task,
            "availableCommands":     "",
            "availableActions":      "",
            "availableTools":        ANUBIX_TOOL_NAMES_CSV,
            "availableToolDetails":  ANUBIX_TOOL_DETAILS,
            "userName":              "",
            "agentName":             self.agent_name,
            "agentPersona":          self.agent_persona,
            "toolCallbackUrl":       self.tool_client.url,
            "allowCodeOutput":       False,
            "allowToolUse":          True,
        }
        # Persistent across rounds within a run (not across separate /api/chat calls).
        self.messages: list[dict[str, Any]] = []

    def _post_chat(self) -> dict:
        payload: dict[str, Any] = {
            "messages":                self.messages,
            "systemInstructionRequest": self.system_instruction_request,
            "agentName":               self.agent_name,
        }
        if self.engine:
            payload["engine"] = self.engine
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        last_error: Optional[str] = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                r = requests.post(
                    CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=self.http_timeout,
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                wait = min(RETRY_BACKOFF_CAP, 2.0 ** attempt)
                if attempt < MAX_API_RETRIES:
                    print(f"{C.YELLOW}[retry {attempt}/{MAX_API_RETRIES}] "
                          f"{last_error} — waiting {wait:.0f}s{C.R}")
                    time.sleep(wait)
                    continue
                break

            if r.ok:
                return r.json()

            # Surface body for both retry-worthy and terminal errors.
            try:
                body = r.json()
            except ValueError:
                body = {}

            # Transient OmniLink errors: server tells us retryAfterSec; honor it.
            retry_worthy = (
                500 <= r.status_code < 600
                or body.get("code") == "PRIMARY_ENGINE_FAILED"
            )
            if retry_worthy and attempt < MAX_API_RETRIES:
                retry_after = (
                    body.get("retryAfterSec")
                    or (body.get("retryAfterMs", 0) or 0) / 1000.0
                    or min(RETRY_BACKOFF_CAP, 2.0 ** attempt)
                )
                err_code = (
                    body.get("code")
                    or body.get("error", {}).get("code")
                    or f"HTTP_{r.status_code}"
                )
                print(f"{C.YELLOW}[retry {attempt}/{MAX_API_RETRIES}] "
                      f"{err_code} (HTTP {r.status_code}) — server says "
                      f"retry after {retry_after:.0f}s{C.R}")
                time.sleep(retry_after)
                last_error = f"{err_code} HTTP {r.status_code}"
                continue

            # Terminal — 4xx, or 5xx after exhausting retries.
            raise RuntimeError(
                f"/api/chat returned HTTP {r.status_code}\n"
                f"  body: {r.text[:1500]}"
            )

        raise RuntimeError(
            f"/api/chat: exhausted {MAX_API_RETRIES} attempts. "
            f"Last error: {last_error}"
        )

    def run(self, user_prompt: str) -> str:
        """Run one full prompt → final-answer loop. Returns the final text."""
        self.messages.append({"role": "user", "content": user_prompt})
        print_user(user_prompt)

        last_tool: Optional[str] = None   # last tool we actually dispatched
        nudge_count = 0                   # consecutive nudges this run

        for round_idx in range(self.max_rounds):
            try:
                data = self._post_chat()
            except RuntimeError as exc:
                print(f"{C.RED}{exc}{C.R}")
                return ""

            text       = data.get("text") or ""
            tool_calls = data.get("toolCalls") or []

            print_assistant_text(text, round_idx)

            # Persist this round's anubix text to public.chats. Skip empty
            # texts (model emitted only tool calls, nothing to log).
            if text.strip() and self.chat_uploader is not None:
                self.chat_uploader.upload(text, session_id=self.agent_name)

            if not tool_calls:
                finish = _finish_reason(data.get("raw", {}))

                # Terminal tools end the run; anything else means we got
                # stalled mid-sequence and should nudge.
                if last_tool in TERMINAL_TOOLS:
                    print(f"{C.DIM}[done] No tool call after {last_tool} "
                          f"(finishReason={finish!r}) — treating as mission "
                          f"end.{C.R}")
                    print_final(text)
                    self.messages.append({"role": "assistant", "content": text})
                    return text

                if nudge_count < MAX_NUDGES:
                    nudge_count += 1
                    print(f"{C.YELLOW}[nudge {nudge_count}/{MAX_NUDGES}] "
                          f"No tool call (last_tool={last_tool!r}, "
                          f"finishReason={finish!r}) — asking model to emit "
                          f"the next step.{C.R}")
                    self.messages.append({"role": "assistant", "content": text})
                    self.messages.append({"role": "user", "content": GENERIC_NUDGE})
                    continue

                # Cap hit — surface diagnostics and stop.
                print(f"{C.RED}[done] Hit {MAX_NUDGES} consecutive nudges with "
                      f"no tool call (finishReason={finish!r}). Treating "
                      f"last text as final.{C.R}")
                print_final(text)
                self.messages.append({"role": "assistant", "content": text})
                return text

            # Got tool calls — reset the nudge gate.
            nudge_count = 0

            # Echo the assistant turn (with tool_calls) into history, exactly
            # like the website does.
            self.messages.append({
                "role":       "assistant",
                "content":    text,
                "tool_calls": tool_calls,
            })

            # Dispatch every tool call this round. `tool_call_id` MUST match
            # what the model returned.
            for i, tc in enumerate(tool_calls):
                tc_id = tc.get("id") or f"call_{round_idx}_{i}"
                name  = tc.get("name") or "<unknown>"
                args  = tc.get("arguments") or {}
                print_tool_call(i, len(tool_calls), name, args)

                ok, result_str = self.tool_client.dispatch(name, args)
                print_tool_result(name, result_str, ok)

                self.messages.append({
                    "role":         "tool",
                    "tool_call_id": tc_id,
                    "name":         name,
                    "content":      result_str,
                })

                last_tool = name

        raise RuntimeError(
            f"Exceeded {self.max_rounds} tool rounds without a final answer. "
            "Either bump --max-rounds or check the agent for a loop."
        )


# ─── SSH tunnel ────────────────────────────────────────────────────────────

class JetsonTunnelError(RuntimeError):
    """Raised when the SSH forward to the Jetson cannot be established."""


class JetsonTunnel:
    """
    Forwards localhost:<local_port> -> <jetson_ip>:<remote_port> over SSH
    using password auth. Closes cleanly on exit.
    """

    # Quick retry loop absorbs the common "tailscale just came up, route not
    # ready yet" race. Past these the Jetson is genuinely unreachable.
    _CONNECT_RETRIES = 3
    _CONNECT_BACKOFF = (2.0, 4.0, 8.0)

    def __init__(
        self,
        host:        str,
        username:    str,
        password:    str,
        ssh_port:    int = DEFAULT_SSH_PORT,
        local_port:  int = DEFAULT_LOCAL_PORT,
        remote_port: int = DEFAULT_REMOTE_PORT,
    ):
        self.host        = host
        self.username    = username
        self.password    = password
        self.ssh_port    = ssh_port
        self.local_port  = local_port
        self.remote_port = remote_port
        self._fwd: Optional[SSHTunnelForwarder] = None
        self._reused_existing = False

    def __enter__(self) -> "JetsonTunnel":
        # If the local port is already in use, assume the user has their own
        # ssh -L running and don't try to bind on top of it.
        if _port_in_use(LOCAL_TOOL_HOST, self.local_port):
            print(
                f"{C.YELLOW}[tunnel] Port {self.local_port} is already in use on "
                f"localhost — assuming an existing SSH forward and reusing it. "
                f"Close whatever owns it if that isn't what you want.{C.R}"
            )
            self._fwd = None
            self._reused_existing = True
            return self

        last_exc: Optional[BaseException] = None
        for attempt in range(1, self._CONNECT_RETRIES + 1):
            try:
                self._fwd = SSHTunnelForwarder(
                    (self.host, self.ssh_port),
                    ssh_username = self.username,
                    ssh_password = self.password,
                    remote_bind_address = ("127.0.0.1", self.remote_port),
                    local_bind_address  = (LOCAL_TOOL_HOST, self.local_port),
                    set_keepalive = 30.0,
                )
                self._fwd.start()
                last_exc = None
                break
            except BaseSSHTunnelForwarderError as exc:
                last_exc = exc
                # Clean up the half-built forwarder before retrying.
                try:
                    if self._fwd is not None:
                        self._fwd.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._fwd = None
                if attempt < self._CONNECT_RETRIES:
                    wait = self._CONNECT_BACKOFF[attempt - 1]
                    print(
                        f"{C.YELLOW}[tunnel] attempt {attempt}/{self._CONNECT_RETRIES} "
                        f"failed: {exc} — retrying in {wait:.0f}s "
                        f"(tailscale route may still be settling){C.R}"
                    )
                    time.sleep(wait)

        if last_exc is not None:
            raise JetsonTunnelError(
                f"SSH tunnel failed after {self._CONNECT_RETRIES} attempts: {last_exc}. "
                f"Host: {self.host}:{self.ssh_port} user: {self.username}. "
                f"Forward: localhost:{self.local_port} -> jetson:{self.remote_port}. "
                f"Check that tailscale is connected on both ends and the Jetson is reachable."
            )

        actual = self._fwd.local_bind_port
        print(
            f"{C.GREEN}[tunnel] SSH forward up: "
            f"127.0.0.1:{actual} -> {self.host}:{self.remote_port} "
            f"(user={self.username}){C.R}"
        )
        self.local_port = actual
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fwd and self._fwd.is_active:
            self._fwd.stop()
            print(f"{C.DIM}[tunnel] closed.{C.R}")


def _port_in_use(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


class JetsonHealthError(RuntimeError):
    """Raised when the Jetson master node /health probe fails."""


def health_check(tool_client: "JetsonToolClient") -> None:
    """
    Real HTTP probe to /health on the Jetson master node. A raw TCP probe is
    useless because the SSH tunnel ALWAYS accepts connections locally — even
    if nothing on the Jetson side is actually serving HTTP. We need to
    confirm the response is from ros_master_node specifically.
    """
    ok, msg = tool_client.health()
    if ok:
        print(f"{C.GREEN}[health] {msg}{C.R}")
        return
    print(f"{C.RED}[health] {msg}{C.R}")
    raise JetsonHealthError(
        "Jetson tool endpoint isn't responding to HTTP. "
        "Dispatching tool calls now would just time out for 180s each. "
        f"Detail: {msg}"
    )


# ─── CLI ───────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ANUBIX OmniLink /api/chat client (laptop side). "
                    "Opens an SSH tunnel to the Jetson and dispatches the "
                    "model's tool calls to it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python anubix_api_client.py --jetson-ip 192.168.1.42 \\
                  --jetson-user anubix --jetson-pass *** \\
                  --prompt "go check disease at x=65 y=75, robot=<uuid>, task=<uuid>"

              # interactive (one prompt per line, /exit to quit)
              python anubix_api_client.py --jetson-ip 192.168.1.42 \\
                  --jetson-user anubix --jetson-pass ***
        """),
    )
    p.add_argument("--jetson-ip",    default=os.environ.get("JETSON_IP", ""),
                   help="Jetson IP/hostname (env JETSON_IP)")
    p.add_argument("--jetson-user",  default=os.environ.get("JETSON_USER", "anubix"),
                   help="SSH username on the Jetson (env JETSON_USER, default 'anubix')")
    p.add_argument("--jetson-pass",  default=os.environ.get("JETSON_PASS", ""),
                   help="SSH password (env JETSON_PASS; prompted if blank)")
    p.add_argument("--jetson-ssh-port", type=int,
                   default=int(os.environ.get("JETSON_SSH_PORT", DEFAULT_SSH_PORT)),
                   help=f"SSH port (env JETSON_SSH_PORT, default {DEFAULT_SSH_PORT})")
    p.add_argument("--remote-port",  type=int, default=DEFAULT_REMOTE_PORT,
                   help=f"Tool-agent port on the Jetson (default {DEFAULT_REMOTE_PORT})")
    p.add_argument("--local-port",   type=int, default=DEFAULT_LOCAL_PORT,
                   help=f"Local port to bind for the forward (default {DEFAULT_LOCAL_PORT})")
    p.add_argument("--prompt",       default=None,
                   help="One-shot prompt. Omit to enter interactive mode.")
    p.add_argument("--engine",       default="g1-engine",
                   help="Engine to use (default: g1-engine — Gemini, the engine the "
                        "tool-call architecture is tested against). Other options: "
                        "g2-engine, g3-engine.")
    p.add_argument("--temperature",  type=float, default=0.1,
                   help="Sampling temperature (default 0.1). Lower = more "
                        "deterministic; helps the model emit tool calls "
                        "consistently instead of stopping after narration.")
    p.add_argument("--share-memory", action="store_true",
                   help="Use a stable agentName='ANUBIX' so this session shares "
                        "memory with the OmniLink website. DEFAULT (off) uses a "
                        "session-unique agentName to bypass server-side memory — "
                        "otherwise server concatenates memory + messages and "
                        "doubles the conversation every round.")
    p.add_argument("--no-chat-upload", action="store_true",
                   help="Skip uploading ANUBIX's text responses to the Supabase "
                        "public.chats table. Default: upload every non-empty "
                        "round (sender='anubix', session_id=agent_name).")
    p.add_argument("--max-rounds",   type=int, default=DEFAULT_MAX_ROUNDS,
                   help=f"Safety cap on tool-loop iterations (default {DEFAULT_MAX_ROUNDS})")
    p.add_argument("--tool-timeout", type=float, default=DEFAULT_TOOL_TIMEOUT,
                   help=f"Per-tool POST timeout, seconds (default {DEFAULT_TOOL_TIMEOUT}). "
                        "ROS feedback (nav_goal, perception, arm, spectrometer) can take "
                        "30-120s per call. Make this >= the master node's feedback_timeout.")
    p.add_argument("--no-tunnel", action="store_true",
                   help="Skip the SSH tunnel — assume something else (e.g. an "
                        "already-running `ssh -L`) is already forwarding "
                        f"localhost:{DEFAULT_LOCAL_PORT} to the Jetson.")
    return p.parse_args()


def _resolve_secrets(args: argparse.Namespace) -> tuple[str, str]:
    api_key = (os.environ.get("OMNI_KEY") or "").strip()
    if not api_key:
        raise SystemExit("ERROR: set OMNI_KEY env var first.\n"
                         "  PowerShell:  $env:OMNI_KEY = 'olink_...'\n"
                         "  cmd:         set OMNI_KEY=olink_...")
    ssh_pass = args.jetson_pass
    if not args.no_tunnel and not ssh_pass:
        ssh_pass = getpass.getpass(f"SSH password for {args.jetson_user}@{args.jetson_ip}: ")
    return api_key, ssh_pass


def _interactive_loop(runner: OmniChatRunner) -> None:
    print(f"{C.DIM}[interactive] Enter prompts. /exit or Ctrl+C to quit.{C.R}")
    while True:
        try:
            prompt = input(f"{C.BLUE}you> {C.R}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not prompt:
            continue
        if prompt in ("/exit", "/quit"):
            return
        try:
            runner.run(prompt)
        except RuntimeError as exc:
            print(f"{C.RED}[error] {exc}{C.R}")


def main() -> None:
    args = parse_args()
    api_key, ssh_pass = _resolve_secrets(args)

    if not args.no_tunnel and not args.jetson_ip:
        raise SystemExit("ERROR: --jetson-ip is required (or set JETSON_IP env var). "
                         "Use --no-tunnel if you've forwarded the port yourself.")

    chat_uploader: Optional[ChatHistoryUploader] = None
    if not args.no_chat_upload:
        try:
            chat_uploader = ChatHistoryUploader(SUPABASE_URL, SUPABASE_KEY)
            print(f"{C.GREEN}[chats] Supabase chat-history upload enabled "
                  f"({SUPABASE_URL}){C.R}")
        except RuntimeError as exc:
            print(f"{C.YELLOW}[chats] disabled — {exc}{C.R}")

    runner_kwargs = dict(
        engine        = args.engine,
        max_rounds    = args.max_rounds,
        temperature   = args.temperature,
        share_memory  = args.share_memory,
        chat_uploader = chat_uploader,
    )

    if args.no_tunnel:
        tool_client = JetsonToolClient(local_port=args.local_port,
                                       timeout=args.tool_timeout)
        health_check(tool_client)
        runner = OmniChatRunner(api_key=api_key, tool_client=tool_client,
                                **runner_kwargs)
        if args.prompt:
            runner.run(args.prompt)
        else:
            _interactive_loop(runner)
        return

    with JetsonTunnel(
        host        = args.jetson_ip,
        username    = args.jetson_user,
        password    = ssh_pass,
        ssh_port    = args.jetson_ssh_port,
        local_port  = args.local_port,
        remote_port = args.remote_port,
    ) as tunnel:
        tool_client = JetsonToolClient(local_port=tunnel.local_port,
                                       timeout=args.tool_timeout)
        health_check(tool_client)
        runner = OmniChatRunner(api_key=api_key, tool_client=tool_client,
                                **runner_kwargs)
        if args.prompt:
            runner.run(args.prompt)
        else:
            _interactive_loop(runner)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.DIM}[exit] interrupted by user.{C.R}")
