# ANUBIX API Client — HTTP service

A thin FastAPI wrapper around `anubix_api_client.OmniChatRunner`.

```
POST /run  ─►  validate (session_id required)  ─►  202 Accepted (immediate)
                                                       │
                                                       ▼
                                  asyncio.Lock (FIFO)  ─►  OmniChatRunner.run()
                                                            ├─ SSH tunnel to Jetson (over tailscale)
                                                            ├─ /api/chat loop (Gemini, retries, nudges)
                                                            └─ optional Supabase chats insert
                                                       │
                                                       ▼
                                               in-memory job registry
                                               (GET /run/{session_id}/status)
                                               + live logs on container stdout
```

The HTTP layer is **fire-and-forget**: POST /run returns 202 with a
`session_id` as soon as the request is validated. The runner streams its
output straight to container stdout (visible via `docker logs -f`), and the
final state lands in the in-memory registry — poll
`GET /run/{session_id}/status` for `done` / `failed`. For durable results,
rely on the Supabase chat-history upload.

The underlying client (`api_client/anubix_api_client.py`) is patched only
to use a `RuntimeError`-based tunnel error (instead of `SystemExit`, which
killed uvicorn handlers) and to retry SSH connect a few times to absorb the
"tailscale just came up" race. `PROMPT_PATH` is also patched at boot so the
prompt is found at `/app/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt` inside the
container.

---

## 1. Setup

```bash
cd anubix/api_client/service
cp .env.example .env
# edit .env, fill in OMNI_KEY + JETSON_PASS (and Supabase if you want defaults)
docker compose build
docker compose up        # foreground; -d for daemonised
```

The first time you run, you have two paths for tailscale auth:

**Non-interactive (recommended for servers):** set
`TAILSCALE_AUTHKEY=tskey-...` in `.env` — the container joins the tailnet
at boot.

**Interactive (no authkey):** boot the container, then:

```bash
curl -X POST http://localhost:6000/tailscale/up -d '{}' -H 'content-type: application/json'
# → { "ok": true, "mode": "interactive",
#     "login_url": "https://login.tailscale.com/a/<token>" }
```

Open the URL in a browser, approve the device. Verify with
`GET /tailscale/status`.

---

## 2. Endpoints

| Verb | Path                          | Purpose |
|------|-------------------------------|---------|
| GET  | `/health`                     | Liveness probe + queue-busy status |
| GET  | `/tailscale/status`           | Backend state, hostname, tailnet IPs |
| POST | `/tailscale/up`               | Trigger auth; returns login URL or "ok" |
| POST | `/run`                        | **202 Accepted**: schedule a mission, return `session_id` immediately |
| GET  | `/run/{session_id}/status`    | Poll a previously-accepted run (`queued` / `running` / `done` / `failed`) |

---

## 3. Request shape

```jsonc
POST /run
{
  "prompt":   "go check disease at x=70 y=75, robot=… task=…",   // ONE OF
  "messages": [                                                  // OR
    { "role": "system",    "content": "…" },
    { "role": "user",      "content": "…" },
    { "role": "assistant", "content": "…" },
    { "role": "user",      "content": "…" }     // last MUST be user
  ],

  // OPTIONAL — identifies the chat.  Replaces the old chat_id.
  // If provided: all Supabase rows share this value, and sending the same
  // session_id again routes to the same OmniLink agent (session affinity).
  // If omitted: a throwaway session is created (fire-and-forget, no
  // history to return to).
  "session_id": "my-chat-uuid-or-slug",

  // OPTIONAL — all three (session_id + user_id + task_id) enable Supabase
  // upload.  Provide all three or omit them.  Mixed = 400.
  "user_id": "uuid",
  "task_id": "uuid",

  // OPTIONAL — runner tuning (defaults match the CLI)
  "engine":       "g1-engine",
  "temperature":  0.1,
  "share_memory": false,
  "max_rounds":   60
}
```

### Response (HTTP 202 — immediate)

```jsonc
{
  "session_id":          "ANUBIX-session-a3f2e1c0",
  "status":              "queued",
  "chat_upload_enabled": true,
  "message":             "Run accepted. Stream output with `docker logs -f`; poll GET /run/{session_id}/status for completion."
}
```

Stream the live transcript on the server with `docker logs -f anubix-api-client`
— it shows exactly what the CLI prints. To check the final state:

```bash
curl http://localhost:6000/run/ANUBIX-session-a3f2e1c0/status
```

```jsonc
{
  "session_id":          "ANUBIX-session-a3f2e1c0",
  "status":              "done",          // queued | running | done | failed
  "final_text":          "All tasks complete. …",
  "error":               null,
  "chat_upload_enabled": true,
  "chat_upload_stats":   { "ok": 8, "failed": 0 },
  "started_at":          1748140200.12,
  "finished_at":         1748140260.45
}
```

> The registry is **in-memory only** — restarts wipe it. Durable history
> lives in Supabase (`public.chats`, filterable by `session_id`).

---

## 4. curl examples

> Run a quick check first: `curl http://localhost:6000/health`
>
> **All `/run` calls return immediately with 202.** Watch the live transcript
> with `docker logs -f anubix-api-client`. Poll
> `GET /run/{session_id}/status` if you need the final state via HTTP.

### a) Single prompt, no upload (fire and forget)

```bash
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "go check if the plant at x=70 and y=75 has any diseases, your robot id is 34a957fd-d45c-4dbf-8e02-be8e1b5e349a, and the task id is 40e4060b-5bc8-4044-9d71-046fee27a757"
  }'
# → { "session_id": "tmp-a3f2e1c0", "status": "queued", … }
# No session_id → throwaway session, no history to return to.
```

### b) Single prompt **with Supabase upload**

```bash
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "abc-chat-002",
    "prompt":  "go check disease at x=70 y=75, robot=34a957fd-d45c-4dbf-8e02-be8e1b5e349a, task=40e4060b-5bc8-4044-9d71-046fee27a757",
    "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "task_id": "99999999-8888-7777-6666-555555555555"
  }'
```

### c) Multi-turn `messages` array **with Supabase upload**

```bash
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "abc-chat-003",
    "messages": [
      { "role": "user",      "content": "what robots can you control?" },
      { "role": "assistant", "content": "I control the ANUBIX agritech robot." },
      { "role": "user",      "content": "great, now check disease at x=12 y=34, robot=34a957fd-d45c-4dbf-8e02-be8e1b5e349a, task=40e4060b-5bc8-4044-9d71-046fee27a757" }
    ],
    "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "task_id": "99999999-8888-7777-6666-555555555555"
  }'
```

### d) Tune the runner (pin a different engine + temperature)

```bash
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "abc-chat-004",
    "prompt":      "what is the robot status?",
    "engine":      "g1-engine",
    "temperature": 0.0,
    "max_rounds":  10
  }'
```

### e) Poll a run until it finishes

```bash
curl -s -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{ "session_id": "poll-test-001", "prompt": "what can you do?" }'

# Poll every 5s. Live transcript still appears in `docker logs -f`.
while true; do
  STATUS=$(curl -s "http://localhost:6000/run/poll-test-001/status" | jq -r .status)
  echo "$(date +%T)  poll-test-001  $STATUS"
  case "$STATUS" in done|failed) break;; esac
  sleep 5
done

curl -s "http://localhost:6000/run/poll-test-001/status" | jq .
```

### f) Emergency stop (bypasses queue)

```bash
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{ "prompt": "stop" }'
# → Immediately dispatches supervisor_force_stop to the Jetson,
#   even if another mission is in flight. Does NOT wait in queue.
# session_id is optional for emergencies.
```

Emergency words: `stop`, `halt`, `abort`, `emergency`, `e-stop`, `estop`,
`force stop`, `emergency stop`, `stop now`, `stop everything`,
`abort mission`, `stop immediately`.

### g) Continue a previous session (session affinity)

```bash
# First request — client provides its own session_id:
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "my-chat-42",
    "prompt": "what can you do?"
  }'
# → { "session_id": "my-chat-42", "status": "queued", … }

# Second request — same session_id routes to the same OmniLink agent
# and restores the full conversation history:
curl -X POST http://localhost:6000/run \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "my-chat-42",
    "prompt": "ok, go check disease at x=70 y=75, robot=34a957fd-d45c-4dbf-8e02-be8e1b5e349a, task=40e4060b-5bc8-4044-9d71-046fee27a757"
  }'
# → The agent remembers the prior exchange and continues from there.
```

### h) Tailscale auth flow

```bash
# 1. Check current state
curl http://localhost:6000/tailscale/status

# 2. Trigger interactive auth (no authkey)
curl -X POST http://localhost:6000/tailscale/up \
  -H 'content-type: application/json' -d '{}'

# 3. Or pass a one-shot authkey on the request
curl -X POST http://localhost:6000/tailscale/up \
  -H 'content-type: application/json' \
  -d '{ "authkey": "tskey-auth-…" }'
```

---

## 5. Concurrency & queueing

`/run` returns 202 immediately. The worker then waits on a single FIFO
`asyncio.Lock`, so two concurrent missions still execute one-at-a-time on
the physical robot. While a run is in flight, `GET /health` reports
`"queue_busy": true`, and the queued job sits at `status: "queued"` until
the lock frees up — then flips to `"running"`.

The runner's live output streams to container stdout — `docker logs -f
anubix-api-client` shows the same view the CLI would print (banners, tool
calls, tool results, nudges).

---

## 6. Session affinity, emergency bypass & no-tool grace

### Session affinity

`session_id` (provided by the client) identifies the chat. Internally the
server generates a separate `agent_name` for OmniLink routing and
maintains a mapping: `session_id → agent_name + messages`.
If `session_id` is omitted, a throwaway `tmp-*` id is generated and the
request is fire-and-forget (no history to return to).

When a request arrives with a `session_id` that has been seen before:
1. The server looks up which `agent_name` was paired with it.
2. It restores the full conversation history from the previous run.
3. It routes the new request to the same OmniLink agent.

When a `session_id` is new, a fresh `agent_name` is generated and the
request is treated as a brand-new conversation.

If the session is still `queued` or `running`, the server returns
`409 Conflict` — wait for it to finish first.

Supabase rows are always grouped by `session_id` (the client's value),
never by the internal `agent_name`.

### Emergency bypass

If the prompt is (or contains) an emergency phrase — `stop`, `halt`,
`abort`, `force stop`, etc. — the request **bypasses the FIFO queue** and
dispatches `supervisor_force_stop` directly to the Jetson tool endpoint.
This works even while another mission is in flight, because the existing
SSH tunnel is already forwarding `localhost:5055`.

### No-tool grace period

The first two rounds of every run allow the model to respond with text only
(no tool calls) without triggering the nudge loop. This means simple
questions like _"what can you do?"_ or _"what is the robot's status?"_ get
a direct text answer instead of the model being pushed to emit a tool call.
From round 3 onward the normal nudge-on-missing-tool-call logic applies.

### Required parameters

The agent prompt enforces that all four parameters — `robot_id`, `task_id`,
`x`, `y` — must be present in the user message before any tool calls are
emitted. If any are missing, the agent replies with text asking the user to
provide them. General questions and emergency stops are exempt.

---

## 7. Secrets handling

| Where it lives | Used for |
|---|---|
| `.env.example` | **Safe to share.** Template only, no real secrets. Commit this. |
| `.env`         | **Never share. Never commit.** Real values, loaded by docker-compose. In `.gitignore` + `.dockerignore`. |
| Docker secrets / k8s secrets / cloud Secret Manager | **Production.** Mount as env vars at runtime; don't bake secrets into the image. |

When sending this project to teammates:

1. Push the repo with `.env.example` only.
2. Give each teammate the real values out-of-band — your team's secrets
   manager (1Password / Doppler / Vault / GitHub Actions secrets / etc.).
3. They copy `.env.example → .env` locally and paste in their assigned values.
4. The image they build is identical to yours; only the runtime env differs.

For the OmniLink key specifically: each operator should have their own
`olink_…` key so usage attribution stays clean. Same for tailscale auth
keys — use per-device, ephemeral, or tagged keys, not your personal login.

---

## 8. Running on a server (AWS EC2 etc.)

### Kernel TUN mode is **mandatory**

The service opens an SSH tunnel to the Jetson over tailscale. `paramiko`
(under `sshtunnel`) makes a direct TCP connection — it does NOT speak
SOCKS5. Tailscale's *userspace-networking* mode only routes traffic that
goes through its SOCKS5 proxy, so without a real TUN device the SSH
connect times out with `Could not establish session to SSH gateway`.

The entrypoint refuses to start without `/dev/net/tun`, so the failure is
loud instead of silent.

Pre-flight on the host (Ubuntu EC2 AMIs already pass these):

```bash
ls -l /dev/net/tun          # → crw-rw-rw- 1 root root 10, 200 …
# if missing:
sudo modprobe tun
```

### Pull and run from Docker Hub

```bash
docker pull abdelrahmanatef01/anubix-api-client:latest

docker run -d \
    --name anubix-api-client \
    --network host \
    --cap-add NET_ADMIN \
    --device /dev/net/tun:/dev/net/tun \
    --env-file /home/ubuntu/.env \
    -v anubix-tailscale-state:/var/lib/tailscale \
    --restart unless-stopped \
    abdelrahmanatef01/anubix-api-client:latest

docker logs -f anubix-api-client
```

> With `--network host` you don't need `-p 6000:6000`; the container is
> already on the host's network namespace.

### Build locally instead

```bash
docker build \
    -f api_client/service/Dockerfile \
    -t anubix-api-client \
    "D:/college_projects/Graduation Project/anubix"   # build context = anubix/ root
```

Then swap the image name in the `docker run` above.
