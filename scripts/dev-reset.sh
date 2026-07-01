#!/usr/bin/env bash
#
# Wipe the ISOLATED dev instance — its config AND all its data — leaving your
# default agent and every box-shared item (the machine-wide gateway config in
# host-config.yaml, the commons/ skill library) completely untouched.
#
# Under the two-tier layout (ADR 0065) one instance is ONE subtree:
# box_root/<id>/ holds BOTH its config/ and every data store, so a reset is a
# single rm -rf of that subtree. The box root itself (host-config.yaml, commons/,
# .instances/, .data-version, cache/) is machine-shared and is NEVER touched — so
# the re-init'd dev still inherits this machine's gateway config.
#
# Stop the dev server first (Ctrl-C the `scripts/dev.sh` process).
#
#   scripts/dev-reset.sh                                # resets instance 'dev'
#   PROTOAGENT_INSTANCE=scratch scripts/dev-reset.sh    # resets a differently-named sandbox
set -euo pipefail

IID="${PROTOAGENT_INSTANCE:-dev}"
# Mirror infra.paths box_root: PROTOAGENT_BOX_ROOT override, else /sandbox in a
# container, else ~/.protoagent. The box root is machine-shared and stays put.
if [ -n "${PROTOAGENT_BOX_ROOT:-}" ]; then BOX="${PROTOAGENT_BOX_ROOT}"
elif [ -d /sandbox ]; then BOX="/sandbox"
else BOX="${HOME}/.protoagent"; fi

echo "Resetting dev instance '${IID}' — your default data and the box-shared"
echo "host-config.yaml/commons under ${BOX} stay untouched."
# One subtree holds BOTH the instance config and every data store (ADR 0065).
rm -rf "${BOX:?}/${IID}"

echo "✓ dev instance '${IID}' wiped. Re-launch a fresh one with scripts/dev.sh."
