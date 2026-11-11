#!/usr/bin/env bash
# Put a release-candidate build of the macOS desktop app on THIS Mac.
#
#   scripts/desktop-install-candidate.sh              # build of origin/main's head
#   scripts/desktop-install-candidate.sh --sha <sha>  # a specific commit already built
#   scripts/desktop-install-candidate.sh --run <id>   # a specific desktop-build run
#   scripts/desktop-install-candidate.sh --download-only
#   scripts/desktop-install-candidate.sh --restore    # put the previous app back
#
# It reuses a successful `desktop-build` TEST run (dispatched with a blank tag, so nothing is
# published) for the commit, or dispatches one on main when the commit is main's head, waits
# for it, downloads the signed + notarized aarch64 DMG, runs scripts/verify-macos-desktop.sh
# on it, then swaps /Applications/protoAgent.app. The app it replaces is kept under
# ~/.protoagent/desktop-backups/, and the commit you are now running is recorded in
# ~/.protoagent/desktop-candidate.json (a frozen app can't ask git which commit it is).
#
# Why no self-update fight: a test build takes its version from pyproject.toml, which on main
# equals the latest published release, so the in-app updater sees nothing newer. When the
# next release publishes, the updater moves you onto it — which is what you want.
#
# Swapping the app restarts every agent the desktop app hosts. The script asks first
# (--yes skips the question).
set -euo pipefail

REPO="protoLabsAI/protoAgent"
WORKFLOW="desktop-build.yml"
TARGET="aarch64-apple-darwin"
APP="/Applications/protoAgent.app"
STATE_DIR="$HOME/.protoagent"
BACKUPS="$STATE_DIR/desktop-backups"
RECORD="$STATE_DIR/desktop-candidate.json"

SHA="" RUN="" DOWNLOAD_ONLY=0 RESTORE=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    --download-only) DOWNLOAD_ONLY=1; shift ;;
    --restore) RESTORE=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
[ "$(uname -s)" = "Darwin" ] || { echo "macOS only." >&2; exit 1; }
command -v gh >/dev/null || { echo "needs the GitHub CLI (gh), signed in." >&2; exit 1; }

confirm() {
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

quit_app() {
  if pgrep -f "$APP/Contents/MacOS/" >/dev/null; then
    echo "Quitting protoAgent (its agents stop with it)…"
    osascript -e 'tell application "protoAgent" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 120); do
      pgrep -f "$APP/Contents/" >/dev/null || return 0
      sleep 1
    done
    echo "Processes from $APP are still running after 2 minutes (pgrep -fl '$APP/Contents/')." >&2
    echo "Quit them yourself and run this again; replacing the app under them is unsafe." >&2
    exit 1
  fi
}

if [ "$RESTORE" = 1 ]; then
  LAST=$(ls -1dt "$BACKUPS"/*.app 2>/dev/null | head -1 || true)
  [ -n "$LAST" ] || { echo "No backup in $BACKUPS." >&2; exit 1; }
  confirm "Restore $(basename "$LAST") to $APP? This restarts every agent the app hosts." || exit 1
  quit_app
  rm -rf "$APP"
  ditto "$LAST" "$APP"
  rm -f "$RECORD"
  open "$APP"
  echo "Restored $(basename "$LAST")."
  exit 0
fi

# 1. Which commit.
if [ -z "$SHA" ] && [ -z "$RUN" ]; then
  SHA=$(gh api "repos/$REPO/commits/main" --jq .sha)
  echo "Candidate: main @ ${SHA:0:12}"
fi

# 2. Which run: given, an existing successful test build for the commit, or a new dispatch.
find_run() {
  gh run list -R "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch --limit 30 \
    --json databaseId,headSha,status,conclusion \
    --jq "[.[] | select(.headSha == \"$SHA\") | select(.conclusion == \"success\" or .status != \"completed\")][0].databaseId // empty"
}
if [ -z "$RUN" ]; then
  RUN=$(find_run)
  if [ -z "$RUN" ]; then
    MAIN=$(gh api "repos/$REPO/commits/main" --jq .sha)
    if [ "$SHA" != "$MAIN" ]; then
      echo "No desktop test build exists for ${SHA:0:12}, and it isn't main's head (${MAIN:0:12})," >&2
      echo "so this script can't dispatch one for it. Dispatch desktop-build on a branch at that" >&2
      echo "commit, then pass --run <id>." >&2
      exit 1
    fi
    echo "Dispatching a desktop test build on main (blank tag: artifacts only, nothing published)…"
    gh workflow run "$WORKFLOW" -R "$REPO" --ref main
    for _ in $(seq 1 30); do
      sleep 5
      RUN=$(find_run)
      [ -n "$RUN" ] && break
    done
    [ -n "$RUN" ] || { echo "The dispatched run didn't appear; check the Actions tab." >&2; exit 1; }
  fi
fi
RUN_SHA=$(gh run view "$RUN" -R "$REPO" --json headSha --jq .headSha)
SHA=${SHA:-$RUN_SHA}
[ "$RUN_SHA" = "$SHA" ] || { echo "Run $RUN built ${RUN_SHA:0:12}, not ${SHA:0:12}." >&2; exit 1; }
echo "Build run: https://github.com/$REPO/actions/runs/$RUN"

# 3. Wait for it (a full desktop build takes a while; the macOS leg is the slow one).
while :; do
  read -r STATUS CONCLUSION < <(gh run view "$RUN" -R "$REPO" --json status,conclusion --jq '"\(.status) \(.conclusion)"')
  [ "$STATUS" = "completed" ] && break
  echo "  build is ${STATUS}… (checking again in 60s)"
  sleep 60
done
[ "$CONCLUSION" = "success" ] || { echo "Run $RUN finished $CONCLUSION; nothing to install." >&2; exit 1; }

# 4. Download + verify the DMG.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/protoagent-candidate.XXXXXX")
gh run download "$RUN" -R "$REPO" --pattern "protoAgent-*-$TARGET" -D "$WORK"
DMG=$(find "$WORK" -name '*.dmg' | head -1)
[ -n "$DMG" ] || { echo "No DMG in run $RUN's $TARGET artifact." >&2; exit 1; }
echo "Verifying $(basename "$DMG")…"
scripts/verify-macos-desktop.sh --require-signed "$DMG"
if [ "$DOWNLOAD_ONLY" = 1 ]; then
  echo "Downloaded and verified: $DMG"
  exit 0
fi

# 5. Swap the installed app.
confirm "Install the ${SHA:0:12} build over $APP? This restarts every agent the app hosts." || exit 1
quit_app
mkdir -p "$BACKUPS"
if [ -d "$APP" ]; then
  OLD=$(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo unknown)
  BK="$BACKUPS/protoAgent-$OLD-$(date +%Y%m%d-%H%M%S).app"
  ditto "$APP" "$BK"
  echo "Kept the previous app at $BK"
fi
MNT=$(mktemp -d "${TMPDIR:-/tmp}/protoagent-dmg.XXXXXX")
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" >/dev/null
trap 'hdiutil detach "$MNT" -force >/dev/null 2>&1 || true' EXIT
rm -rf "$APP"
ditto "$MNT/protoAgent.app" "$APP"
hdiutil detach "$MNT" -force >/dev/null
trap - EXIT
VER=$(defaults read "$APP/Contents/Info" CFBundleShortVersionString)
printf '{"sha":"%s","run":%s,"version":"%s","installed_at":"%s"}\n' \
  "$SHA" "$RUN" "$VER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RECORD"
open "$APP"
echo "Installed protoAgent $VER built from ${SHA:0:12} (run $RUN). Recorded in $RECORD."
echo "Undo with: scripts/desktop-install-candidate.sh --restore"
