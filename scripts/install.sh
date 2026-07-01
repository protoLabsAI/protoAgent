#!/bin/sh
# protoAgent — one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh
#
# Takes a fresh machine from zero to a running, configured protoAgent:
#   1. checks prerequisites (Docker + curl)
#   2. pulls ghcr.io/protolabsai/protoagent:latest
#   3. runs it (loopback-published, named volume, restart:unless-stopped)
#   4. drives a config wizard that hits the SAME /api/config/* endpoints the
#      browser setup wizard uses — so it stays in parity for free
#   5. writes the config and prints where the agent is running
#
# Re-running is safe: it pulls the latest image, preserves the data volume, and
# offers to re-run the wizard. Works over a plain SSH session (no browser); when
# there's no TTY it starts the container and points you at the console to finish.
#
# POSIX sh on purpose (the `| sh` entrypoint) — no bashisms.
#
# Overridable via env:
#   PROTOAGENT_IMAGE       image ref            (default ghcr.io/protolabsai/protoagent:latest)
#   PROTOAGENT_CONTAINER   container name       (default protoagent)
#   PROTOAGENT_VOLUME      data volume name     (default protoagent-sandbox)
#   PROTOAGENT_PORT        host port            (default 7870)
#   PROTOAGENT_BIND        host bind address    (default 127.0.0.1 — loopback)
#   PROTOAGENT_DEFAULT_API_BASE / _DEFAULT_MODEL   wizard defaults
#   PROTOAGENT_INSTALL_URL         configure an already-running instance; skip Docker
#   PROTOAGENT_INSTALL_NONINTERACTIVE=1   never prompt (start only; finish in browser)
#   A2A_AUTH_TOKEN         bearer for a non-loopback bind (see docs)

set -eu

# ── Config ────────────────────────────────────────────────────────────────
IMAGE=${PROTOAGENT_IMAGE:-ghcr.io/protolabsai/protoagent:latest}
CONTAINER=${PROTOAGENT_CONTAINER:-protoagent}
VOLUME=${PROTOAGENT_VOLUME:-protoagent-sandbox}
PORT=${PROTOAGENT_PORT:-7870}
BIND=${PROTOAGENT_BIND:-127.0.0.1}
DEFAULT_API_BASE=${PROTOAGENT_DEFAULT_API_BASE:-https://api.proto-labs.ai/v1}
DEFAULT_MODEL=${PROTOAGENT_DEFAULT_MODEL:-protolabs/reasoning}
TARGET_URL=${PROTOAGENT_INSTALL_URL:-}
# Health check + printed URLs must target the address the port is actually
# published on ($BIND), not a hardcoded loopback — a specific-IP bind isn't
# reachable via 127.0.0.1. A 0.0.0.0 bind IS reachable via loopback.
HOST=$BIND
[ "$BIND" = "0.0.0.0" ] && HOST=127.0.0.1
BASE_URL="http://${HOST}:${PORT}"

# The published image is currently linux/amd64. On a non-amd64 host (Apple
# Silicon, arm64 servers) a bare `docker pull` errors on the missing native
# manifest, so target amd64 explicitly — Docker Desktop then runs it under
# emulation. Override with PROTOAGENT_PLATFORM (e.g. once a native image exists).
PLATFORM=${PROTOAGENT_PLATFORM:-}
if [ -z "$PLATFORM" ]; then
  case "$(uname -m)" in
    x86_64|amd64) PLATFORM='' ;;
    *)            PLATFORM='linux/amd64' ;;
  esac
fi

# ── Output helpers ────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_R=$(printf '\033[31m'); C_G=$(printf '\033[32m'); C_Y=$(printf '\033[33m')
  C_B=$(printf '\033[36m'); C_0=$(printf '\033[0m')
else
  C_R=''; C_G=''; C_Y=''; C_B=''; C_0=''
fi
say()  { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
good() { printf '%s ✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s ! %s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s ✗ %s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

# ── TTY / interactivity ───────────────────────────────────────────────────
# `curl | sh` feeds the script on stdin, so prompts must read /dev/tty.
if [ -r /dev/tty ]; then TTY=/dev/tty; else TTY=; fi
if [ -n "$TTY" ] && [ "${PROTOAGENT_INSTALL_NONINTERACTIVE:-0}" != 1 ]; then
  INTERACTIVE=1
else
  INTERACTIVE=0
fi

# ── Small helpers ─────────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not found. $2"; }

# ask VAR "Question" "default"  — reads from /dev/tty, falls back to default.
ask() {
  if [ -n "$3" ]; then _ask_p="$2 [$3]: "; else _ask_p="$2: "; fi
  printf '%s' "$_ask_p" > "$TTY"
  IFS= read -r _ask_ans < "$TTY" || _ask_ans=
  [ -z "$_ask_ans" ] && _ask_ans=$3
  eval "$1=\$_ask_ans"
}

# ask_secret VAR "Prompt"  — no echo.
ask_secret() {
  printf '%s: ' "$2" > "$TTY"
  # Restore echo (and exit) if the user Ctrl-C's mid-entry — otherwise the
  # terminal is left echo-off until `reset`.
  trap 'stty echo < "$TTY" 2>/dev/null; exit 130' INT TERM
  stty -echo < "$TTY" 2>/dev/null || true
  IFS= read -r _sec < "$TTY" || _sec=
  stty echo < "$TTY" 2>/dev/null || true
  trap - INT TERM
  printf '\n' > "$TTY"
  _sec=$(printf '%s' "$_sec" | tr -d '\r')  # drop a stray CR from a CRLF paste
  eval "$1=\$_sec"
}

# JSON: minimal, dependency-free (no jq). Values we read are simple scalars.
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
json_str()    { printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n1; }
json_true()   { printf '%s' "$1" | grep -q "\"$2\"[[:space:]]*:[[:space:]]*true"; }
json_models() {
  printf '%s' "$1" \
    | sed -n 's/.*"models"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' \
    | tr ',' '\n' | sed 's/^[[:space:]]*"//; s/"[[:space:]]*$//' | sed '/^$/d'
}

# HTTP: never return nonzero (would trip `set -e`); status lands in HTTP_CODE.
HTTP_CODE=000
HTTP_BODY=""
http_get() {
  if _r=$(curl -sS -m "${2:-20}" -w '\n%{http_code}' "$1" 2>/dev/null); then
    HTTP_CODE=$(printf '%s' "$_r" | tail -n1)
    HTTP_BODY=$(printf '%s' "$_r" | sed '$d')
  else
    HTTP_CODE=000; HTTP_BODY=""
  fi
  return 0
}
http_post_json() {
  if _r=$(printf '%s' "$2" | curl -sS -m "${3:-60}" -H 'Content-Type: application/json' \
            --data-binary @- -w '\n%{http_code}' "$1" 2>/dev/null); then
    HTTP_CODE=$(printf '%s' "$_r" | tail -n1)
    HTTP_BODY=$(printf '%s' "$_r" | sed '$d')
  else
    HTTP_CODE=000; HTTP_BODY=""
  fi
  return 0
}

# ── Docker ────────────────────────────────────────────────────────────────
run_container() {
  PLAT_ARG=''
  if [ -n "$PLATFORM" ]; then
    PLAT_ARG="--platform $PLATFORM"
    warn "Host is $(uname -m); running the ${PLATFORM} image under emulation (slower first boot)."
  fi
  info "Pulling ${IMAGE} (this can take a minute) …"
  # shellcheck disable=SC2086  # PLAT_ARG must word-split into two args (or none)
  docker pull $PLAT_ARG "$IMAGE" >/dev/null || die "docker pull failed for ${IMAGE}"
  if docker ps -a --format '{{.Names}}' | grep -Fqx "$CONTAINER"; then
    info "Replacing existing '${CONTAINER}' container — the '${VOLUME}' data volume is kept."
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  info "Starting '${CONTAINER}' on ${BIND}:${PORT} …"
  # PROTOAGENT_UI=console flips the baked headless image to serve the console
  # (assets always ship). ALLOW_OPEN=1: the in-container 0.0.0.0 bind is fenced
  # by the loopback port publish — same posture as the bundled docker-compose.
  # shellcheck disable=SC2086  # PLAT_ARG must word-split into two args (or none)
  docker run -d $PLAT_ARG \
    --name "$CONTAINER" \
    --restart unless-stopped \
    -p "${BIND}:${PORT}:7870" \
    -v "${VOLUME}:/sandbox" \
    -e PROTOAGENT_UI=console \
    -e PROTOAGENT_ALLOW_OPEN=1 \
    -e "A2A_AUTH_TOKEN=${A2A_AUTH_TOKEN:-}" \
    --security-opt no-new-privileges:true \
    "$IMAGE" >/dev/null || die "docker run failed — see: docker logs ${CONTAINER}"
}

wait_ready() {
  info "Waiting for protoAgent to accept connections …"
  _i=0
  while [ "$_i" -lt 90 ]; do
    # setup-status answers 200 pre-configuration; /healthz only goes 200 AFTER
    # setup (the graph compiles then), so it's the wrong readiness signal here.
    http_get "${BASE_URL}/api/config/setup-status" 5
    [ "$HTTP_CODE" = 200 ] && { good "Server is up."; return 0; }
    _i=$((_i + 1)); sleep 2
  done
  die "protoAgent did not come up in time. Inspect: docker logs ${CONTAINER}"
}

# ── Wizard (mirrors apps/web SetupWizard over the same endpoints) ──────────
wizard() {
  http_get "${BASE_URL}/api/config/setup-status" 10
  [ "$HTTP_CODE" = 200 ] || die "Cannot reach ${BASE_URL} (HTTP ${HTTP_CODE})."

  if json_true "$HTTP_BODY" setup_complete; then
    if [ "$INTERACTIVE" = 1 ]; then
      ask _RE "protoAgent is already configured — re-run the setup wizard? [y/N]" ""
      case "$_RE" in
        y|Y|yes|YES) : ;;
        *) info "Keeping the existing configuration."; return 0 ;;
      esac
    else
      info "protoAgent is already configured; leaving its settings untouched."
      return 0
    fi
  fi

  if [ "$INTERACTIVE" != 1 ]; then
    warn "No interactive terminal — skipping the config wizard."
    say  "Finish setup in a browser at ${BASE_URL}/app, or re-run this script from a terminal."
    return 0
  fi

  say ""
  info "Let's configure your agent."
  ask AGENT_NAME "Agent name" "protoagent"
  ask API_BASE   "Model gateway URL (OpenAI-compatible)" "$DEFAULT_API_BASE"
  ask_secret API_KEY "API key (blank if your gateway needs none)"

  info "Fetching available models …"
  http_post_json "${BASE_URL}/api/config/models" \
    "{\"api_base\":\"$(json_escape "$API_BASE")\",\"api_key\":\"$(json_escape "$API_KEY")\"}" 30
  MODELS=$(json_models "$HTTP_BODY")
  MODEL=""
  if [ -n "$MODELS" ]; then
    say "Available models:"
    printf '%s\n' "$MODELS" | awk '{ printf "  %2d) %s\n", NR, $0 }' > "$TTY"
    _count=$(printf '%s\n' "$MODELS" | wc -l | tr -d ' ')
    ask _PICK "Choose a number, or type a model name" "1"
    _PICK=$(printf '%s' "$_PICK" | tr -d '[:space:]')  # so " 2" still reads as choice 2
    if printf '%s' "$_PICK" | grep -q '^[0-9][0-9]*$' && \
       [ "$_PICK" -ge 1 ] && [ "$_PICK" -le "$_count" ]; then
      MODEL=$(printf '%s\n' "$MODELS" | sed -n "${_PICK}p")
    else
      MODEL=$_PICK
    fi
  else
    _perr=$(json_str "$HTTP_BODY" error)
    [ -n "$_perr" ] && warn "Could not list models: ${_perr}"
    ask MODEL "Model name" "$DEFAULT_MODEL"
  fi
  [ -z "$MODEL" ] && MODEL=$DEFAULT_MODEL

  info "Testing '${MODEL}' …"
  http_post_json "${BASE_URL}/api/config/test-model" \
    "{\"api_base\":\"$(json_escape "$API_BASE")\",\"api_key\":\"$(json_escape "$API_KEY")\",\"model\":\"$(json_escape "$MODEL")\"}" 60
  if json_true "$HTTP_BODY" ok; then
    good "Connection OK."
  else
    _terr=$(json_str "$HTTP_BODY" error)
    warn "Connection test failed: ${_terr:-unknown error}"
    ask _CONT "Save this configuration anyway? [y/N]" ""
    case "$_CONT" in
      y|Y|yes|YES) : ;;
      *) die "Aborted — re-run the script to try again." ;;
    esac
  fi

  info "Writing configuration …"
  _key=""
  [ -n "$API_KEY" ] && _key=",\"api_key\":\"$(json_escape "$API_KEY")\""
  _cfg="{\"config\":{\"agent_runtime\":\"native\",\"model\":{\"provider\":\"openai\",\"name\":\"$(json_escape "$MODEL")\",\"api_base\":\"$(json_escape "$API_BASE")\"${_key}},\"identity\":{\"name\":\"$(json_escape "$AGENT_NAME")\"}}}"
  http_post_json "${BASE_URL}/api/config/setup" "$_cfg" 120
  if json_true "$HTTP_BODY" ok; then
    good "Setup complete."
  else
    _merr=$(json_str "$HTTP_BODY" message)
    warn "Setup did not complete cleanly: ${_merr:-unknown}. Finish it in the console at ${BASE_URL}/app."
  fi
}

success() {
  say ""
  say "${C_G}  protoAgent is running.${C_0}"
  say "  Console:  ${BASE_URL}/app"
  say "  API:      ${BASE_URL}/v1      (OpenAI-compatible)"
  say "  A2A:      ${BASE_URL}/a2a"
  if [ -z "$TARGET_URL" ]; then
    say ""
    say "  Manage it:"
    say "    docker logs -f ${CONTAINER}     # follow logs"
    say "    docker stop ${CONTAINER}        # stop"
    say "    docker start ${CONTAINER}       # start"
    say "    re-run this installer            # update to the latest image"
    say ""
    say "  Data persists in the '${VOLUME}' Docker volume."
  fi
  say ""
}

main() {
  case "${1:-}" in
    -h|--help) sed -n '2,40p' "$0" 2>/dev/null || say "See the header of scripts/install.sh."; exit 0 ;;
  esac
  need curl "Install curl and re-run."
  if [ -n "$TARGET_URL" ]; then
    BASE_URL=$TARGET_URL
    info "Configuring the protoAgent already running at ${BASE_URL} (skipping Docker)."
  else
    need docker "Install Docker — https://docs.docker.com/get-docker/ — and re-run."
    docker info >/dev/null 2>&1 || die "Docker is installed but its daemon isn't running. Start Docker and re-run."
    # ALLOW_OPEN=1 is only safe when the loopback publish is the fence. Any other
    # bind exposes the agent on the network, so require a bearer token there —
    # otherwise it's open + unauthenticated.
    case "$BIND" in
      127.0.0.1|localhost) : ;;
      *) [ -n "${A2A_AUTH_TOKEN:-}" ] || die "PROTOAGENT_BIND=${BIND} publishes the agent on the network. Set A2A_AUTH_TOKEN (e.g. A2A_AUTH_TOKEN=\$(openssl rand -hex 24)) and re-run, or keep the default loopback bind." ;;
    esac
    run_container
    wait_ready
  fi
  wizard
  success
}

main "$@"
