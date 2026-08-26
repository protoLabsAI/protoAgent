#!/usr/bin/env bash
# Compatibility wrapper for source checkouts. The packaged/frozen command is
# `protoagent reset`; both paths share the Python implementation and infra.paths.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" -m server reset "$@"
