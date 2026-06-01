#!/usr/bin/env bash
# Create a protoAgent sandbox under a running OpenShell gateway (ADR 0008).
#
# Prereqs: `docker compose -f compose.yml up -d` (gateway running) and the
# `openshell` CLI installed + registered against the gateway:
#   openshell gateway add http://127.0.0.1:8080 --local --name local
#
# This (1) generates a least-privilege policy from your protoAgent config and
# (2) creates the sandbox. The policy's filesystem paths come from
# `filesystem.projects` and the egress allowlist from `egress.allowed_hosts`
# + `model.api_base` — so the sandbox can only touch what the agent is actually
# configured to use.
#
# NOTE: `openshell sandbox create` flags (image/mounts/ports) vary by release —
# the call below is a template; adapt mount/port flags to your OpenShell CLI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="${PROTOAGENT_CONFIG:-$REPO_ROOT/config/langgraph-config.yaml}"
IMAGE="${PROTOAGENT_IMAGE:-ghcr.io/protolabsai/protoagent:latest}"
POLICY="${POLICY_OUT:-$REPO_ROOT/deploy/openshell/openshell-policy.yaml}"
PORT="${PROTOAGENT_PORT:-7870}"

echo "→ generating OpenShell policy from $CONFIG"
python "$REPO_ROOT/scripts/gen_openshell_policy.py" --config "$CONFIG" --out "$POLICY"

echo "→ creating protoAgent sandbox (image=$IMAGE, port=$PORT)"
# The gateway applies the policy (Landlock fs + seccomp + deny-by-default egress)
# to the container it spins for this command. Mount the live config + the data
# volume; the project dirs are mounted per the policy's filesystem paths.
openshell sandbox create \
  --name protoagent \
  --policy "$POLICY" \
  --image "$IMAGE" \
  --publish "127.0.0.1:${PORT}:7870" \
  --mount "$CONFIG:/sandbox/config/langgraph-config.yaml:ro" \
  -- python server.py --port 7870

echo "✓ protoAgent sandbox created. Inspect: openshell sandbox list"
