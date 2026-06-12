#!/usr/bin/env bash
# Verify a built protoAgent desktop DMG — structure always; the signing /
# notarization / entitlements battery when the app inside is Developer
# ID-signed (release builds). Ported from the ORBIS release pipeline: assert on
# the PRISTINE artifact that ships (the app inside the mounted DMG), not the
# build tree.
#
# Usage:
#   scripts/verify-macos-desktop.sh [--require-signed] <path/to/app.dmg>
#
# --require-signed   Fail if the app is not Developer ID-signed (semver
#                    releases); without it an unsigned dev build passes the
#                    structure checks and skips the signing battery.
set -euo pipefail

REQUIRE_SIGNED=0
DMG=""
for arg in "$@"; do
  case "$arg" in
    --require-signed) REQUIRE_SIGNED=1 ;;
    *) DMG="$arg" ;;
  esac
done
[ -n "$DMG" ] && [ -f "$DMG" ] || { echo "FAIL: DMG not found: ${DMG:-<missing>}"; exit 2; }

MOUNT="$(mktemp -d /tmp/protoagent-dmg-XXXXXX)"
hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT" "$DMG" >/dev/null
trap 'hdiutil detach "$MOUNT" >/dev/null 2>&1 || true' EXIT

APP="$(ls -d "$MOUNT"/*.app | head -1)"
[ -d "$APP" ] || { echo "FAIL: no .app inside the DMG"; exit 1; }
echo "ok: app inside DMG: $(basename "$APP")"

# ── Structure (always) ───────────────────────────────────────────────────────
EXEC_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")"
MAIN="$APP/Contents/MacOS/$EXEC_NAME"
[ -x "$MAIN" ] || { echo "FAIL: main executable missing/not executable: $MAIN"; exit 1; }
file "$MAIN" | grep -q "arm64" || { echo "FAIL: main executable is not arm64"; exit 1; }
echo "ok: main executable present (arm64)"

# Tauri strips the target-triple suffix from externalBin at bundle time.
SIDECAR="$APP/Contents/MacOS/protoagent-server"
[ -x "$SIDECAR" ] || { echo "FAIL: bundled sidecar missing/not executable: $SIDECAR"; exit 1; }
SIZE="$(stat -f%z "$SIDECAR")"
[ "$SIZE" -gt 1000000 ] || { echo "FAIL: sidecar suspiciously small (${SIZE} bytes)"; exit 1; }
file "$SIDECAR" | grep -q "arm64" || { echo "FAIL: sidecar is not arm64"; exit 1; }
echo "ok: sidecar bundled (arm64, $((SIZE / 1024 / 1024)) MB)"

# ── Signing mode ─────────────────────────────────────────────────────────────
# (-dvv, not -dv: the Authority= certificate chain only prints at verbosity 2+.)
if ! codesign -dvv "$APP" 2>&1 | grep -q "Authority=Developer ID Application:"; then
  if [ "$REQUIRE_SIGNED" = "1" ]; then
    echo "FAIL: app is not Developer ID-signed and --require-signed was set"
    exit 1
  fi
  echo "ok: unsigned dev build — skipping the signing/notarization battery"
  exit 0
fi

# ── Signing + notarization battery (release builds) ─────────────────────────
codesign --verify --deep --strict --verbose=2 "$APP"
echo "ok: codesign verify (deep, strict)"
codesign -dvv "$APP" 2>&1 | grep -q "TeamIdentifier=" || { echo "FAIL: no TeamIdentifier"; exit 1; }
echo "ok: Developer ID authority + team identifier"

ENTITLEMENTS="$(codesign -d --entitlements :- "$APP" 2>/dev/null)"
# disable-library-validation is REQUIRED: the PyInstaller sidecar dlopen()s a
# Python.framework with a different Team ID, which the hardened runtime blocks
# without it (the sidecar — hence the whole backend — was DOA in v0.35.0).
for key in com.apple.security.network.client com.apple.security.network.server \
           com.apple.security.cs.allow-jit com.apple.security.cs.disable-library-validation; do
  echo "$ENTITLEMENTS" | grep -q "$key" || { echo "FAIL: missing entitlement: $key"; exit 1; }
done
# Still keep the blast radius tight — nothing broader than the declared set.
for key in com.apple.security.cs.allow-unsigned-executable-memory \
           com.apple.security.cs.allow-dyld-environment-variables; do
  if echo "$ENTITLEMENTS" | grep -q "$key"; then
    echo "FAIL: forbidden entitlement present: $key"
    exit 1
  fi
done
echo "ok: entitlements exactly as declared (network client/server + JIT + library-validation exception for the frozen sidecar; nothing broader)"

spctl --assess --type execute --verbose=4 "$APP"
echo "ok: Gatekeeper accepts the app"
xcrun stapler validate "$APP"
echo "ok: notarization ticket stapled to the app"

# ── DMG container ────────────────────────────────────────────────────────────
spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG"
echo "ok: Gatekeeper accepts the DMG"
xcrun stapler validate "$DMG"
echo "ok: notarization ticket stapled to the DMG"

echo
echo "MACOS DESKTOP ARTIFACT VERIFIED ✓"
