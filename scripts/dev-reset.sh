#!/usr/bin/env bash
#
# Wipe the ISOLATED dev instance's state — its config + ALL its data — leaving your
# default ("prod") agent under ~/.protoagent and config/ completely untouched.
#
# Stop the dev server first (Ctrl-C the `scripts/dev.sh` process).
#
#   scripts/dev-reset.sh                       # resets instance 'dev'
#   PROTOAGENT_INSTANCE=scratch scripts/dev-reset.sh   # resets a differently-named sandbox
set -euo pipefail
cd "$(dirname "$0")/.."

IID="${PROTOAGENT_INSTANCE:-dev}"
DATA="${HOME}/.protoagent"

echo "Resetting dev instance '${IID}' — your prod data under ${DATA} stays untouched."
# Scoped config (seeded from the default on first run): config/<iid>/{langgraph-config,secrets,.setup-complete}
rm -rf "config/${IID}"
# Per-instance data root: ~/.protoagent/<iid>
rm -rf "${DATA:?}/${IID}"
# Per-store scoped leaves: ~/.protoagent/<store>/<iid>  (tasks, knowledge, inbox, activity, scheduler, background, …)
find "${DATA}" -mindepth 2 -maxdepth 2 -type d -name "${IID}" -exec rm -rf {} + 2>/dev/null || true

echo "✓ dev instance '${IID}' wiped. Re-launch a fresh one with scripts/dev.sh."
