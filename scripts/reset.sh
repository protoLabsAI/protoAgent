#!/usr/bin/env bash
#
# Factory-reset the DEFAULT ("prod") protoAgent instance: wipe its data + local
# config back to a clean slate so the next boot runs the setup wizard. For local
# testing of the fresh-user flow. (Reset ONLY the dev sandbox instead with
# scripts/dev-reset.sh; there is intentionally NO in-app factory reset — #1159.)
#
# SAFE ON A MULTI-INSTANCE MACHINE — it preserves EVERY OTHER instance:
#   * any  ~/.protoagent/<name>  that carries an instance marker (.instance-uid /
#     checkpoints.db) is left untouched (the dev sandbox, fleet members, forks);
#   * inside a shared store dir (knowledge/, memory/, tasks/, …) every
#     <store>/<instance> leaf (a SUBDIRECTORY) is preserved — only prod's own
#     unscoped top-level DBs and the direct files in those store dirs are removed.
# Shipped/tracked files in config/ are RESTORED to pristine (git checkout); only
# gitignored local config is deleted.
#
#   scripts/reset.sh --dry-run       # print exactly what would change; delete nothing
#   scripts/reset.sh                 # confirm, then reset prod (keeps every other instance)
#   scripts/reset.sh --yes           # skip the typed confirmation
#   scripts/reset.sh --backup        # timestamped tar.gz of data + config first
#   scripts/reset.sh --keep-secrets  # keep secrets.yaml + langgraph-config.yaml (no re-auth)
#   scripts/reset.sh --include-dev   # ALSO wipe the `dev` sandbox (its data + config/dev)
#   scripts/reset.sh --force         # stop a server still bound to the port first
#
# ALWAYS run --dry-run first on a busy machine and read the plan.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

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
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ── paths ─────────────────────────────────────────────────────────────────────
# Mirror infra.paths.data_home(): /sandbox in a container, else ~/.protoagent.
if [ -d /sandbox ]; then DATA="/sandbox"; else DATA="${HOME}/.protoagent"; fi
CONFIG="${PROTOAGENT_CONFIG_DIR:-${REPO}/config}"
PORT="${PORT:-7870}"

# Gitignored local config to DELETE (the prod instance's machine-local state).
CONFIG_IGNORED=(langgraph-config.yaml secrets.yaml .setup-complete plugins)
# Shipped/tracked config to RESTORE to pristine (git checkout, never delete).
CONFIG_TRACKED=(config/SOUL.md config/skills config/plugin-catalog.json
                config/mcp-catalog.json config/soul-presets config/langgraph-config.example.yaml
                plugins.lock)

# ── helpers ───────────────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
plan() { printf '  %s\n' "$*"; }
run() { if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

is_instance_root() { [ -e "$1/.instance-uid" ] || [ -e "$1/checkpoints.db" ]; }

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

# ── 1. the plan ───────────────────────────────────────────────────────────────
KEEP="dev"; $INCLUDE_DEV && KEEP=""
say ""
say "Factory reset — DEFAULT (prod) instance"
say "  data:   ${DATA}"
say "  config: ${CONFIG}"
$DRY_RUN && say "  mode:   DRY RUN (nothing will be deleted)"
say ""

if [ -d "$DATA" ]; then
  say "Data home (${DATA}):"
  PRESERVED=()
  while IFS= read -r entry; do
    name="$(basename "$entry")"
    if [ -d "$entry" ] && is_instance_root "$entry"; then
      if [ -n "$KEEP" ] || [ "$name" != "dev" ]; then PRESERVED+=("$name"); continue; fi
    fi
    if [ -f "$entry" ] || [ -L "$entry" ]; then
      plan "delete prod file:  ${name}"
    elif [ -d "$entry" ]; then
      # A store dir: prod's content is its DIRECT FILES; every subdir is a
      # <store>/<instance> leaf and is preserved (unless --include-dev → drop dev/).
      files=$(find "$entry" -mindepth 1 -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')
      leaves=$(find "$entry" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
      plan "store dir ${name}/: drop ${files} prod file(s); keep ${leaves} instance leaf(s)$( $INCLUDE_DEV && [ -d "$entry/dev" ] && echo ' (minus dev/)')"
    fi
  done < <(find "$DATA" -mindepth 1 -maxdepth 1 | sort)
  [ ${#PRESERVED[@]} -gt 0 ] && plan "PRESERVE other instances: ${PRESERVED[*]}"
else
  plan "(no data home at ${DATA})"
fi

say ""
say "Config (${CONFIG}):"
for f in "${CONFIG_IGNORED[@]}"; do
  if $KEEP_SECRETS && { [ "$f" = "secrets.yaml" ] || [ "$f" = "langgraph-config.yaml" ]; }; then
    [ -e "${CONFIG}/${f}" ] && plan "keep (--keep-secrets): ${f}"
    continue
  fi
  [ -e "${CONFIG}/${f}" ] && plan "delete local:  ${f}"
done
$INCLUDE_DEV && [ -e "${CONFIG}/dev" ] && plan "delete dev config:  dev/"
for p in "${CONFIG_TRACKED[@]}"; do plan "restore tracked: ${p}"; done

if $DRY_RUN; then
  say ""
  say "Dry run complete — nothing was changed."
  exit 0
fi

# ── 2. confirm ────────────────────────────────────────────────────────────────
if ! $ASSUME_YES; then
  say ""
  printf "Type 'reset' to wipe the prod instance (other instances are preserved): "
  read -r reply
  [ "$reply" = "reset" ] || { say "aborted."; exit 1; }
fi

# ── 3. backup ─────────────────────────────────────────────────────────────────
if $DO_BACKUP; then
  TS="$(date +%Y%m%d-%H%M%S)"
  BK="${HOME}/protoagent-backup-${TS}.tar.gz"
  say "▶ backing up data + config → ${BK}"
  tar czf "$BK" -C "$HOME" "$(realpath --relative-to="$HOME" "$DATA" 2>/dev/null || echo .protoagent)" \
      -C "$REPO" config 2>/dev/null || say "  (backup best-effort; some paths skipped)"
fi

# ── 4. wipe prod data (preserving every other instance) ───────────────────────
if [ -d "$DATA" ]; then
  while IFS= read -r entry; do
    name="$(basename "$entry")"
    if [ -d "$entry" ] && is_instance_root "$entry"; then
      if [ -n "$KEEP" ] || [ "$name" != "dev" ]; then continue; fi
    fi
    if [ -f "$entry" ] || [ -L "$entry" ]; then
      run rm -f "$entry"
    elif [ -d "$entry" ]; then
      # delete prod's direct files; preserve every <store>/<instance> subdir
      while IFS= read -r f; do run rm -f "$f"; done < <(find "$entry" -mindepth 1 -maxdepth 1 -type f)
      if $INCLUDE_DEV && [ -d "$entry/dev" ]; then run rm -rf "$entry/dev"; fi
      rmdir "$entry" 2>/dev/null || true  # remove if now empty (held no instance leaf)
    fi
  done < <(find "$DATA" -mindepth 1 -maxdepth 1 | sort)
fi
# --include-dev: also drop the dev instance root + its scoped data leaves.
if $INCLUDE_DEV; then
  run rm -rf "${DATA:?}/dev"
  while IFS= read -r leaf; do run rm -rf "$leaf"; done \
    < <(find "$DATA" -mindepth 2 -maxdepth 2 -type d -name dev 2>/dev/null)
fi

# ── 5. config: delete gitignored local state, restore tracked to pristine ─────
for f in "${CONFIG_IGNORED[@]}"; do
  if $KEEP_SECRETS && { [ "$f" = "secrets.yaml" ] || [ "$f" = "langgraph-config.yaml" ]; }; then continue; fi
  [ -e "${CONFIG}/${f}" ] && run rm -rf "${CONFIG:?}/${f}"
done
$INCLUDE_DEV && [ -e "${CONFIG}/dev" ] && run rm -rf "${CONFIG:?}/dev"
# Restore the shipped/tracked files only if they're tracked in this repo.
for p in "${CONFIG_TRACKED[@]}"; do
  git -C "$REPO" ls-files --error-unmatch "$p" >/dev/null 2>&1 && run git -C "$REPO" checkout -- "$p"
done

say ""
say "✓ prod instance reset. Next boot (python -m server) runs the setup wizard."
