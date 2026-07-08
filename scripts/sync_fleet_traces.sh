#!/usr/bin/env bash
#
# Sync fleet trace-export dumps → the lab's dataset dir (the flywheel Observe, #1897).
#
# The trace exporter (observability/trace_export.py, gated by
# PROTOAGENT_FLEET_TRACE_EXPORT) writes one daily JSONL per instance at
#   $PROTOAGENT_HOME/<instance>/fleet-traces/fleet-traces-YYYYMMDD.jsonl
# This ships those dumps to a shared dataset dir protoLab ingests via
# dataset/adapters.py::_fleet. Meant to run daily from cron.
#
# Every fleet member emits an identically-named daily file, so dest filenames are
# namespaced by instance (`<instance>__fleet-traces-YYYYMMDD.jsonl`) — flat dir,
# collision-free, provenance in the name (each row also carries `teacher`).
#
#   FLEET_TRACES_DEST            dest dir (default /mnt/data/datasets/fleet-traces)
#   PROTOAGENT_HOME             box root to scan (default ~/.protoagent)
#   FLEET_TRACES_EXTRA_SOURCES  extra ':'-separated fleet-traces dirs (e.g. host
#                               mount points for the containerized prod fleet)
#
# Dumps are append-only + daily-partitioned: past days are immutable, only today's
# file grows, so a plain re-copy each run is idempotent and safe.
set -euo pipefail

DEST="${FLEET_TRACES_DEST:-/mnt/data/datasets/fleet-traces}"
BOX_ROOT="${PROTOAGENT_HOME:-$HOME/.protoagent}"

mkdir -p "$DEST"

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
    rsync -a "$f" "$DEST/${inst}__$(basename "$f")"
    synced=$((synced + 1))
  done
done

echo "$(date -Iseconds) [sync_fleet_traces] synced $synced dump(s) from ${#sources[@]} source(s) -> $DEST"
