#!/usr/bin/env bash
#
# Launch an ISOLATED dev instance of protoAgent so testing never touches your real
# ("prod") agent data.
#
# Uses PROTOAGENT_INSTANCE scoping (ADR 0004): the dev instance is its own instance
# root at ~/.protoagent/dev (config under ~/.protoagent/dev/config, plugins under
# ~/.protoagent/dev/plugins) with FRESH, separate chat / tasks / knowledge /
# checkpoint data. Your default instance (~/.protoagent/default, port 7870) is left
# completely untouched.
#
#   scripts/dev.sh                 # → PROTOAGENT_INSTANCE=dev on http://127.0.0.1:7871
#   PORT=7882 scripts/dev.sh       # pick a different port
#   PROTOAGENT_INSTANCE=scratch scripts/dev.sh   # a differently-named sandbox
#   scripts/dev.sh --ui none       # extra args pass straight through to `python -m server`
#
# Reset just this sandbox (prod untouched):  scripts/dev-reset.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PROTOAGENT_INSTANCE="${PROTOAGENT_INSTANCE:-dev}"
PORT="${PORT:-7871}"

# Prefer the uv-managed project venv; fall back to whatever `python` is on PATH.
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python"

echo "▶ protoAgent dev instance '${PROTOAGENT_INSTANCE}' on http://127.0.0.1:${PORT}"
echo "  isolated config+data (seeded from your default config; prod data untouched)"
exec "$PY" -m server --port "$PORT" "$@"
