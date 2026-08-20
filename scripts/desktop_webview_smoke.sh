#!/usr/bin/env bash
# Boot the bundled Linux desktop app against a REAL GL compositor and assert it
# stays up and serves its console.
#
# Why this exists: `live_smoke.py --bin` boots the frozen sidecar only. Nothing
# rendered the webview, so a crash that killed the app on every launch could ship
# green — which is exactly what happened (#2866).
#
# Why weston and NOT xvfb-run: the crash fires whenever WebKitGTK fails to create
# an accelerated backing store and 2.52 dereferences the null anyway (upstream
# https://bugs.webkit.org/show_bug.cgi?id=321683). Under `xvfb-run` on a box whose
# EGL loader can select a GPU vendor driver, that is the DEFAULT outcome — the
# smoke would fail 100% of the time for reasons unrelated to the build. weston's headless backend with the GL renderer gives real
# accelerated compositing over Mesa llvmpipe — no GPU, no DRI device needed —
# which is what a GH-hosted runner can actually provide. Verified on a runner-like
# box with every /dev/dri/* unreadable: GL renderer = llvmpipe, app survives.
#
# Usage: scripts/desktop_webview_smoke.sh <path-to-AppImage> [soak-seconds]
set -euo pipefail

APP_PATH="${1:?usage: desktop_webview_smoke.sh <AppImage> [soak-seconds]}"
SOAK="${2:-60}"

[ -x "$APP_PATH" ] || chmod +x "$APP_PATH"

WORK="$(mktemp -d)"
export XDG_RUNTIME_DIR="$WORK/xdg"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
WESTON_LOG="$WORK/weston.log"
APP_LOG="$WORK/app.log"
WESTON_PID=""
APP_PID=""

cleanup() {
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "$WESTON_PID" ] && kill "$WESTON_PID" 2>/dev/null || true
  sleep 1
  rm -rf "$WORK"
}
trap cleanup EXIT

# Pin the EGL vendor to Mesa so the compositor is deterministic and never grabs a
# discrete GPU that's busy with something else.
#
# ⚠️ This ALSO suppresses the crash class this smoke exists to catch, on any box
# where glvnd can hand WebKit the NVIDIA driver. Measured, same AppImage, same
# 45s soak:
#
#     Xvfb  + default EGL (NVIDIA installed) -> CRASH  (segfault at 48)
#     Xvfb  + forced Mesa EGL                -> pass
#     weston + GL (Mesa)                     -> pass
#
# In CI that's moot — hosted runners have Mesa only, so this is inert there. On a
# workstation with NVIDIA drivers it is NOT moot: leaving it on can turn a real
# reproduction into a false pass. Set SMOKE_FORCE_MESA=0 to reproduce what a user
# on that box actually gets.
if [ "${SMOKE_FORCE_MESA:-1}" = "1" ] && [ -f /usr/share/glvnd/egl_vendor.d/50_mesa.json ]; then
  export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json
  export LIBGL_ALWAYS_SOFTWARE=1
fi
export LIBSEAT_BACKEND=noop

echo "==> starting weston (headless + GL)"
weston --backend=headless --renderer=gl --width=1600 --height=1000 \
       --socket=wayland-smoke --xwayland >"$WESTON_LOG" 2>&1 &
WESTON_PID=$!

for _ in $(seq 1 60); do
  grep -q "xserver listening on display" "$WESTON_LOG" && break
  kill -0 "$WESTON_PID" 2>/dev/null || { echo "FAIL: weston exited"; cat "$WESTON_LOG"; exit 1; }
  sleep 1
done
grep -q "xserver listening on display" "$WESTON_LOG" || {
  echo "FAIL: weston never brought up Xwayland"; cat "$WESTON_LOG"; exit 1; }

# Fail loudly if we fell back to a software *rasterizer without GL* — the whole
# point is to exercise the accelerated-compositing path.
grep -E "GL renderer|Using GL renderer" "$WESTON_LOG" || {
  echo "FAIL: weston is not using the GL renderer"; cat "$WESTON_LOG"; exit 1; }
sed -n 's/.*\(GL renderer: .*\)/==> \1/p' "$WESTON_LOG"

DISPLAY_NUM="$(sed -n 's/.*xserver listening on display \(:[0-9]*\).*/\1/p' "$WESTON_LOG" | head -1)"
echo "==> Xwayland on ${DISPLAY_NUM}"

echo "==> launching ${APP_PATH##*/}"
env DISPLAY="$DISPLAY_NUM" GDK_BACKEND=x11 "$APP_PATH" >"$APP_LOG" 2>&1 &
APP_PID=$!

# The shell pins 7870 when free, else falls back to an OS-assigned port and hands
# it to the page via ?__apiPort (#1668) — read whichever it chose.
PORT=7870
for _ in $(seq 1 90); do
  if grep -qE "sidecar on [0-9]+" "$APP_LOG"; then
    PORT="$(sed -n 's/.*sidecar on \([0-9]*\).*/\1/p' "$APP_LOG" | head -1)"; break
  fi
  grep -q "Starting protoagent" "$APP_LOG" && break
  kill -0 "$APP_PID" 2>/dev/null || { echo "FAIL: app exited during startup"; tail -40 "$APP_LOG"; exit 1; }
  sleep 1
done
echo "==> sidecar port ${PORT}"

for _ in $(seq 1 90); do
  curl -sf -o /dev/null "http://127.0.0.1:${PORT}/app" && break
  kill -0 "$APP_PID" 2>/dev/null || { echo "FAIL: app exited before serving /app"; tail -40 "$APP_LOG"; exit 1; }
  sleep 1
done
BODY="$(curl -sf "http://127.0.0.1:${PORT}/app" || true)"
[ -n "$BODY" ] || { echo "FAIL: /app never returned 200"; tail -40 "$APP_LOG"; exit 1; }
# A 200 alone proves a server is listening, not that it served the console. The
# SPA shell must reference its own bundle, or we're smoking an error page.
grep -q '/app/assets/' <<<"$BODY" || {
  echo "FAIL: /app responded but the document is not the console shell"
  printf '%s\n' "${BODY:0:400}"; tail -40 "$APP_LOG"; exit 1; }
echo "==> console served (SPA shell verified)"

# The crash this guards against lands a few seconds AFTER the console renders, so
# a boot check alone would miss it. Soak.
echo "==> soaking ${SOAK}s"
for _ in $(seq 1 "$SOAK"); do
  kill -0 "$APP_PID" 2>/dev/null || {
    echo "FAIL: app died during soak — the webview crash class (#2866)"
    tail -40 "$APP_LOG"; exit 1; }
  sleep 1
done

curl -sf "http://127.0.0.1:${PORT}/app" | grep -q '/app/assets/' || {
  echo "FAIL: console stopped serving its shell during soak"; tail -40 "$APP_LOG"; exit 1; }

echo "PASS: app alive ${SOAK}s after render, console serving on ${PORT}"
