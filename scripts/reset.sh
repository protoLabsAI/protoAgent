#!/usr/bin/env bash
#
# Factory-reset the DEFAULT protoAgent instance: wipe its single subtree —
# box_root/default/ (its config/ AND every data store) — back to a clean slate so
# the next boot runs the setup wizard. For local testing of the fresh-user flow.
# (Reset ONLY the dev sandbox instead with scripts/dev-reset.sh; there is
# intentionally NO in-app factory reset — #1159.)
#
# Two-tier layout (ADR 0065): one instance is ONE subtree, so a reset is a wipe of
# box_root/default. This is SAFE on a multi-instance / shared machine — it preserves
#   * every BOX-shared item (machine-wide): host-config.yaml (the gateway/Host
#     config), commons/, .instances/, .data-version, cache/; and
#   * every OTHER instance subtree (box_root/<name>, name != default) — the dev
#     sandbox, fleet members, forks. --include-dev ALSO wipes box_root/dev.
# Live config now lives under box_root/default/config, never in the repo tree, so
# the old "restore tracked config/ via git checkout" step is gone.
#
#   scripts/reset.sh --dry-run       # print exactly what would change; delete nothing
#   scripts/reset.sh                 # confirm, then reset default (keeps every other instance)
#   scripts/reset.sh --yes           # skip the typed confirmation
#   scripts/reset.sh --backup        # timestamped tar.gz of default + host-config first
#   scripts/reset.sh --keep-secrets  # keep config/{secrets.yaml,langgraph-config.yaml} (no re-auth)
#   scripts/reset.sh --include-dev   # ALSO wipe the `dev` sandbox subtree
#   scripts/reset.sh --force         # stop a server still bound to the port first
#
# ALWAYS run --dry-run first on a busy machine and read the plan.
set -euo pipefail

# ── flags ─────────────────────────────────────────────────────────────────────
DRY_RUN=false; ASSUME_YES=false; DO_BACKUP=false
KEEP_SECRETS=false; INCLUDE_DEV=false; FORCE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=true ;;
    --yes|-y) ASSUME_YES=true ;;
    --backup) DO_BACKUP=true ;;
    --keep-secrets) KEEP_SECRETS=true ;;
    --include-dev) INCLUDE_DEV=true ;;
    --force) FORCE=true ;;
    -h|--help) sed -n '2,/^set -euo/{/^set -euo/!p;}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ── paths ─────────────────────────────────────────────────────────────────────
# Mirror infra.paths box_root: PROTOAGENT_BOX_ROOT override, else /sandbox in a
# container, else ~/.protoagent. box_root is the machine-shared base; the default
# instance is the subtree box_root/default (config + every store live under it).
if [ -n "${PROTOAGENT_BOX_ROOT:-}" ]; then BOX="${PROTOAGENT_BOX_ROOT}"
elif [ -d /sandbox ]; then BOX="/sandbox"
else BOX="${HOME}/.protoagent"; fi
DEFAULT="${BOX}/default"
PORT="${PORT:-7870}"

# Box-tier shared items — machine-wide, NEVER wiped by a single-instance reset.
BOX_SHARED=(host-config.yaml commons .instances .data-version cache)

# ── helpers ───────────────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
plan() { printf '  %s\n' "$*"; }
run()  { if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

is_box_shared() {
  local n="$1" s
  for s in "${BOX_SHARED[@]}"; do [ "$n" = "$s" ] && return 0; done
  return 1
}

# A server still bound to PORT holds WAL handles — a wipe is pointless until it exits.
running_pids() { command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true; }

# ── 0. running-server guard ───────────────────────────────────────────────────
PIDS="$(running_pids)"
if [ -n "$PIDS" ]; then
  if $FORCE && ! $DRY_RUN; then
    say "▶ stopping server on :${PORT} (pids: ${PIDS//$'\n'/ })"
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -n "$(running_pids)" ] || break; sleep 0.5; done
    # shellcheck disable=SC2046
    [ -z "$(running_pids)" ] || kill -9 $(running_pids) 2>/dev/null || true
  elif ! $DRY_RUN; then
    say "✗ a server is bound to :${PORT} (pids: ${PIDS//$'\n'/ })."
    say "  Stop it first, or pass --force to SIGTERM it. (A live process holds WAL handles.)"
    exit 1
  else
    plan "NOTE: a server is bound to :${PORT} (pids: ${PIDS//$'\n'/ }) — stop it (or --force) before a real run."
  fi
fi

# ── 1. classify what lives under the box root ─────────────────────────────────
SHARED=(); OTHERS=()
if [ -d "$BOX" ]; then
  while IFS= read -r entry; do
    name="$(basename "$entry")"
    [ "$name" = "default" ] && continue                 # the wipe target
    $INCLUDE_DEV && [ "$name" = "dev" ] && continue     # also wiped (listed below)
    if is_box_shared "$name"; then SHARED+=("$name"); else OTHERS+=("$name"); fi
  done < <(find "$BOX" -mindepth 1 -maxdepth 1 | sort)
fi

# ── 2. the plan ───────────────────────────────────────────────────────────────
say ""
say "Factory reset — DEFAULT instance"
say "  box:      ${BOX}"
say "  instance: ${DEFAULT}"
$DRY_RUN && say "  mode:     DRY RUN (nothing will be deleted)"
say ""

say "Wipe (config + every data store in the subtree):"
if [ -d "$DEFAULT" ]; then plan "delete instance: ${DEFAULT}"; else plan "(no default instance at ${DEFAULT})"; fi
$INCLUDE_DEV && [ -d "${BOX}/dev" ] && plan "delete instance: ${BOX}/dev  (--include-dev)"
if $KEEP_SECRETS; then
  for f in secrets.yaml langgraph-config.yaml; do
    [ -f "${DEFAULT}/config/${f}" ] && plan "keep (--keep-secrets): config/${f}"
  done
fi
say ""

say "Preserve (box-shared, machine-wide):"
if [ ${#SHARED[@]} -gt 0 ]; then for s in "${SHARED[@]}"; do plan "$s"; done; else plan "(none present)"; fi
say ""
say "Preserve (other instances):"
if [ ${#OTHERS[@]} -gt 0 ]; then for o in "${OTHERS[@]}"; do plan "$o"; done; else plan "(none)"; fi

if $DRY_RUN; then
  say ""
  say "Dry run complete — nothing was changed."
  exit 0
fi

# ── 3. confirm ────────────────────────────────────────────────────────────────
if ! $ASSUME_YES; then
  say ""
  printf "Type 'reset' to wipe the default instance (other instances + box-shared config are preserved): "
  read -r reply
  [ "$reply" = "reset" ] || { say "aborted."; exit 1; }
fi

# ── 4. backup ─────────────────────────────────────────────────────────────────
if $DO_BACKUP; then
  TS="$(date +%Y%m%d-%H%M%S)"
  BK="${HOME}/protoagent-backup-${TS}.tar.gz"
  say "▶ backing up default instance + host-config → ${BK}"
  BK_ITEMS=()
  [ -d "$DEFAULT" ] && BK_ITEMS+=("default")
  [ -e "${BOX}/host-config.yaml" ] && BK_ITEMS+=("host-config.yaml")
  if [ ${#BK_ITEMS[@]} -gt 0 ]; then
    tar czf "$BK" -C "$BOX" "${BK_ITEMS[@]}" 2>/dev/null || say "  (backup best-effort; some paths skipped)"
  else
    say "  (nothing to back up)"
  fi
fi

# ── 5. wipe the default instance (whole subtree), optionally keeping creds ─────
if [ -d "$DEFAULT" ]; then
  if $KEEP_SECRETS; then
    # Stash the two cred files (same fs, preserving 0600), wipe, restore into a
    # fresh empty config/ — no .setup-complete, so next boot still runs the wizard.
    STASH="$(mktemp -d "${BOX}/.reset-keep.XXXXXX")"
    for f in secrets.yaml langgraph-config.yaml; do
      [ -f "${DEFAULT}/config/${f}" ] && cp -p "${DEFAULT}/config/${f}" "${STASH}/${f}"
    done
    run rm -rf "${DEFAULT:?}"
    mkdir -p "${DEFAULT}/config"
    for f in secrets.yaml langgraph-config.yaml; do
      [ -f "${STASH}/${f}" ] && mv "${STASH}/${f}" "${DEFAULT}/config/${f}"
    done
    rmdir "$STASH" 2>/dev/null || true
  else
    run rm -rf "${DEFAULT:?}"
  fi
fi
# --include-dev: also drop the dev sandbox subtree (box-shared items still kept).
if $INCLUDE_DEV && [ -d "${BOX}/dev" ]; then run rm -rf "${BOX:?}/dev"; fi

say ""
say "✓ default instance reset. Next boot (python -m server) runs the setup wizard."
