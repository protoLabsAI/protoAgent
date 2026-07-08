#!/usr/bin/env bash
#
# Sync fleet trace-export dumps → the lab's dataset dir (the flywheel Observe, #1897).
#
# The trace exporter (observability/trace_export.py, gated by
# PROTOAGENT_FLEET_TRACE_EXPORT) writes one daily JSONL per instance at
#   $PROTOAGENT_HOME/<instance>/fleet-traces/fleet-traces-YYYYMMDD.jsonl
# Raw dumps stay on-box; this REDACTS them (scripts/redact_fleet_traces.py:
# regex secrets + openai/privacy-filter PII) and ships only the redacted rows to
# the shared dataset dir protoLab ingests via dataset/adapters.py::_fleet. Meant
# to run daily from cron.
#
# FAIL-CLOSED: if the redactor is unavailable, a source is SKIPPED rather than
# shipping raw PII to the shared corpus. Set FLEET_REDACT=0 to copy raw (only for
# a PII-free / high-trust source, e.g. the dev instance).
#
# Every fleet member emits an identically-named daily file, so dest filenames are
# namespaced by instance (`<instance>__fleet-traces-YYYYMMDD.jsonl`) — flat dir,
# collision-free, provenance in the name (each row also carries `teacher`).
#
#   FLEET_TRACES_DEST            dest dir (default /mnt/data/datasets/fleet-traces)
#   PROTOAGENT_HOME             box root to scan (default ~/.protoagent)
#   FLEET_TRACES_EXTRA_SOURCES  extra ':'-separated fleet-traces dirs (e.g. host
#                               mount points for the containerized prod fleet)
#   FLEET_REDACT                "0" copies raw (no redaction); default redacts
#   FLEET_REDACT_PY             redactor venv python (default
#                               ~/.fleet-trace-redactor/venv/bin/python)
#   FLEET_REDACT_SCRIPT         path to redact_fleet_traces.py (default: alongside)
#
# Dumps are append-only + daily-partitioned: past days are immutable, only today's
# file grows, so re-processing each run is idempotent.
set -euo pipefail

DEST="${FLEET_TRACES_DEST:-/mnt/data/datasets/fleet-traces}"
BOX_ROOT="${PROTOAGENT_HOME:-$HOME/.protoagent}"
REDACT="${FLEET_REDACT:-1}"
REDACT_PY="${FLEET_REDACT_PY:-$HOME/.fleet-trace-redactor/venv/bin/python}"
REDACT_SCRIPT="${FLEET_REDACT_SCRIPT:-$(dirname "$(readlink -f "$0")")/redact_fleet_traces.py}"
# Pin the HF cache so cron (which doesn't inherit the shell's HF_HOME) reuses the
# already-downloaded privacy-filter weights instead of re-fetching ~3GB.
export HF_HOME="${FLEET_REDACT_HF_HOME:-${HF_HOME:-/mnt/data/huggingface/misc}}"

mkdir -p "$DEST"

# Fail-closed guard: unless raw copy is explicitly requested, the redactor MUST be
# present — otherwise we'd ship unredacted prod PII to the shared training corpus.
if [ "$REDACT" != "0" ] && { [ ! -x "$REDACT_PY" ] || [ ! -f "$REDACT_SCRIPT" ]; }; then
  echo "$(date -Iseconds) [sync_fleet_traces] FATAL: redactor unavailable (PY=$REDACT_PY SCRIPT=$REDACT_SCRIPT) — refusing to ship raw PII. Set FLEET_REDACT=0 to override for a PII-free source." >&2
  exit 1
fi

# Collect candidate fleet-traces source dirs: every instance root under the box,
# plus any explicit extras (container volume mounts).
sources=()
for src in "$BOX_ROOT"/*/fleet-traces; do
  [ -d "$src" ] && sources+=("$src")
done
if [ -n "${FLEET_TRACES_EXTRA_SOURCES:-}" ]; then
  IFS=':' read -ra extra <<<"$FLEET_TRACES_EXTRA_SOURCES"
  for src in "${extra[@]}"; do
    [ -d "$src" ] && sources+=("$src")
  done
fi

synced=0
for src in "${sources[@]}"; do
  inst="$(basename "$(dirname "$src")")"
  for f in "$src"/fleet-traces-*.jsonl; do
    [ -e "$f" ] || continue
    dest="$DEST/${inst}__$(basename "$f")"
    if [ "$REDACT" = "0" ]; then
      rsync -a "$f" "$dest"                      # raw copy — PII-free/high-trust source only
    else
      "$REDACT_PY" "$REDACT_SCRIPT" "$f" "$dest" # redact on the way to the shared corpus
    fi
    synced=$((synced + 1))
  done
done

echo "$(date -Iseconds) [sync_fleet_traces] synced $synced dump(s) from ${#sources[@]} source(s) -> $DEST (redact=$([ "$REDACT" = 0 ] && echo off || echo on))"
