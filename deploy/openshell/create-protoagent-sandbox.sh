#!/usr/bin/env bash
# Create a protoAgent sandbox under a running OpenShell gateway (ADR 0008).
#
# Prereqs (see README.md for the full validated walkthrough):
#   - gateway up:  docker compose -f compose.yml up -d   (with its env set)
#   - CLI registered:  openshell gateway add http://127.0.0.1:8080 --local --name local
#   - an agent image built from this repo (the Dockerfile ships iproute2,
#     which the OpenShell supervisor needs for its egress network namespace)
#
# This (1) generates a least-privilege policy from your protoAgent config and
# (2) creates the sandbox. The policy's filesystem paths come from
# `filesystem.projects` and the egress allowlist from `egress.allowed_hosts`
# + `model.api_base` — so the sandbox can only touch what the agent is actually
# configured to use.
#
# Flags below are OpenShell v0.0.59: the image is `--from`, file mounts are
# `--upload` (one-shot copy, not a live bind), and env is `--env` — the
# supervisor does NOT inherit the image's ENV, so PYTHONPATH must be passed
# explicitly. The command after `--` runs in the foreground of this terminal
# (Ctrl-C stops the agent); wrap in tmux/systemd for long-running use.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="${PROTOAGENT_CONFIG:-$REPO_ROOT/config/langgraph-config.yaml}"
IMAGE="${PROTOAGENT_IMAGE:-ghcr.io/protolabsai/protoagent:latest}"
POLICY="${POLICY_OUT:-$REPO_ROOT/deploy/openshell/openshell-policy.yaml}"
PORT="${PROTOAGENT_PORT:-7870}"

echo "→ generating OpenShell policy from $CONFIG"
python "$REPO_ROOT/scripts/gen_openshell_policy.py" --config "$CONFIG" --out "$POLICY"

echo "→ creating protoAgent sandbox (image=$IMAGE, port=$PORT)"
# The gateway applies the policy (Landlock fs + deny-by-default egress proxy +
# unprivileged process identity) to the container it spins for this command.
openshell sandbox create \
  --name protoagent \
  --policy "$POLICY" \
  --from "$IMAGE" \
  --upload "$CONFIG:/sandbox/config/langgraph-config.yaml" \
  --env PYTHONPATH=/opt/protoagent \
  --env "PROTOAGENT_INSTANCE=${PROTOAGENT_INSTANCE:-openshell}" \
  --forward "127.0.0.1:${PORT}" \
  --no-auto-providers \
  -- python -m server --host 127.0.0.1 --port "$PORT" --ui none
  # server/ package (ADR 0023). Loopback bind is correct here: the --forward
  # relay runs inside the sandbox netns, and the server refuses 0.0.0.0
  # without an A2A auth token. Model credentials: pass --env OPENAI_API_KEY
  # (or attach an OpenShell provider) — headless setup fails without one.
