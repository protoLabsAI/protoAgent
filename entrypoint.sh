#!/bin/bash
# protoAgent — container entrypoint
# Secrets should be injected by `infisical run` (or similar) wrapping
# this script. See the deployment stack for the exact invocation.

set -e

AGENT_NAME="${AGENT_NAME:-protoagent}"

echo "[entrypoint] Starting ${AGENT_NAME}"

# tmpfs home — create dirs inside it
mkdir -p /home/sandbox/.local

# Persistent volume dirs (mounted by the stack)
mkdir -p /sandbox/audit /sandbox/knowledge

# Copy persona into workspace if one is shipped
if [ -f /opt/protoagent/config/SOUL.md ]; then
    cp /opt/protoagent/config/SOUL.md /sandbox/SOUL.md
fi

# ADR 0023: server.py was promoted to a `server/` package. Launch it as a
# module with the install dir on PYTHONPATH so the package (and its sibling
# top-level modules: paths, events, graph, …) resolve, while keeping the
# agent's workspace (/sandbox) as the working directory.
#
# Bind all interfaces inside the container — the boundary is the published port
# + network policy, not the in-container bind. (The server defaults to loopback
# for local/desktop runs; PROTOAGENT_HOST overrides either way.)
#
# NOTE: a non-loopback bind with no A2A_AUTH_TOKEN refuses to start unless
# PROTOAGENT_ALLOW_OPEN=1 — the deployment artifact (compose/k8s) must either
# set a token or explicitly opt in after fencing the port. The bundled
# docker-compose.yml publishes to 127.0.0.1 only and opts in; a raw
# `docker run -p 7870:7870` with no token is exactly the case the gate blocks.
exec env PYTHONPATH="/opt/protoagent${PYTHONPATH:+:$PYTHONPATH}" \
    python -m server --host "${PROTOAGENT_HOST:-0.0.0.0}"
