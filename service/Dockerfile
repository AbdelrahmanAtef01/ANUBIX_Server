# syntax=docker/dockerfile:1.6
#
# Build CONTEXT must be the `anubix/` root so this file can COPY both
# api_client/ and anubix_ws/agent_config/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt.
#
# Build:  docker compose build         (from api_client/service/)
# Or:     docker build -f api_client/service/Dockerfile -t anubix-api-client \
#             "D:/college_projects/Graduation Project/anubix"

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: tailscale, ssh client (for the SSH tunnel paramiko uses),
# iptables/iproute2 only needed if TUN mode is enabled at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        openssh-client \
        iptables \
        iproute2 \
        procps \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (api_client base + service).
COPY api_client/requirements.txt /tmp/api_client_requirements.txt
COPY api_client/service/requirements.txt /tmp/service_requirements.txt
RUN pip install -r /tmp/api_client_requirements.txt \
 && pip install -r /tmp/service_requirements.txt

# App code. The api_client module sits at /app/ and the service wrapper at
# /app/service/. server.py adds /app to sys.path so `import anubix_api_client`
# works.
COPY api_client/anubix_api_client.py /app/anubix_api_client.py
COPY api_client/service/             /app/service/

# Prompt — same content the registered ANUBIX profile uses. server.py
# overrides PROMPT_PATH to this location.
COPY anubix_ws/agent_config/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt \
     /app/ANUBIX_AGENT_PROMPT_v3_TOOLCALLS.txt

RUN chmod +x /app/service/entrypoint.sh

EXPOSE 6000

ENTRYPOINT ["/app/service/entrypoint.sh"]
