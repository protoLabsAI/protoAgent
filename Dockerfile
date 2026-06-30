# ---------------------------------------------------------------------------
# Stage 1 — node builder: build the React operator console (apps/web → dist/).
#
# `apps/web/dist` is .gitignored (build output, never committed), so the final
# image can't just `COPY` it from the context — it has to be BUILT here. Without
# this stage the `console` tier 404s at /app: mount_react_app finds no
# dist/index.html and silently returns False (the #874 bug). We build only the
# `@protoagent/web` workspace; `npm ci` at the root resolves the npm workspaces
# (web + desktop), but the desktop workspace only pulls a JS CLI, so the install
# stays light and these node_modules never reach the final image.
# ---------------------------------------------------------------------------
FROM node:20-slim AS web-builder
WORKDIR /build
# Copy only what the web build needs (lockfile-first so this layer caches across
# source-only churn): the workspace manifests + lockfile, then the app sources.
# The root + both workspace package.json files are required for `npm ci` to
# reconstruct the workspace tree the committed package-lock.json describes.
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/
COPY apps/desktop/package.json apps/desktop/
RUN npm ci
# `prebuild` (check-css-comments + copy-plugin-kit) + `build` (tsc typecheck +
# vite build) — emits apps/web/dist/, including dist/_ds/ from the plugin-kit.
COPY apps/web/ apps/web/
RUN npm run build --workspace @protoagent/web

# ---------------------------------------------------------------------------
# Stage 2 — python runtime: the agent core + the built console copied in.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# System deps. iproute2 is required when running under NVIDIA OpenShell —
# the sandbox supervisor shells out to `ip` to build the network namespace
# that enforces deny-by-default egress (sandbox creation fails without it).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential gettext-base gnupg iproute2 \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — optional for forks that call `gh` from tools. Kept in the
# template because almost every agent in the protoLabs fleet ends up
# using it, and the extra ~40MB is cheap compared to rebuilding a layer
# later.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# beads_rust (`br`) — the DAG-aware issue tracker the optional `project_board`
# plugin shells out to. The core task board is in-process (no `br`); this binary
# is kept in the image so the opt-in plugin works out of the box rather than
# failing at runtime. We pull a pinned, checksum-verified prebuilt binary instead of
# installing the Rust toolchain, keeping the slim base small. dpkg's amd64 /
# arm64 map straight onto the release asset arch names; bump BEADS_VERSION to
# upgrade. Source: https://github.com/Dicklesworthstone/beads_rust
ARG BEADS_VERSION=v0.1.23
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64|arm64) ;; \
      *) echo "unsupported arch for br: $arch" >&2; exit 1 ;; \
    esac; \
    asset="br-${BEADS_VERSION}-linux_${arch}.tar.gz"; \
    base="https://github.com/Dicklesworthstone/beads_rust/releases/download/${BEADS_VERSION}"; \
    curl -fsSL -o "/tmp/${asset}" "${base}/${asset}"; \
    curl -fsSL -o "/tmp/${asset}.sha256" "${base}/${asset}.sha256"; \
    (cd /tmp && sha256sum -c "${asset}.sha256"); \
    tar -xzf "/tmp/${asset}" -C /usr/local/bin br; \
    chmod +x /usr/local/bin/br; \
    rm -f "/tmp/${asset}" "/tmp/${asset}.sha256"; \
    br --version

# Non-root sandbox user
ARG SANDBOX_UID=1001
RUN useradd -m -s /bin/bash -u ${SANDBOX_UID} sandbox

# Python deps — installed from requirements.txt so the runtime image stays
# in lockstep with local + CI installs. A hand-maintained list here drifts
# (it had silently lost `croniter`, which the scheduler imports). Copy just
# the requirements first so this layer stays cached across source-only
# changes. Forks that need extras (agent-browser, sqlite-vec, pyjwt[crypto])
# add them to requirements.txt.
# UI tier (ADR 0010): the build-arg bakes PROTOAGENT_UI so the image runs the
# matching tier — default 'none' (API + A2A + /metrics only, the lean headless
# image) or 'console' (also serves the React console, built in the web-builder
# stage above and copied in below as static assets, not a pip dep). Either tier
# uses the same lean core deps — and the console dist always ships regardless of
# UI — so the install is unconditional; forks that need extras (the
# google/Discord MCP surfaces, agent-browser, …) add them to
# requirements-core.txt (note above).
ARG UI=none
COPY requirements*.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-core.txt

# Single COPY with a matching .dockerignore covers everything that
# should ship and excludes .git/, tests/, docs, and dev state. Adding a
# new top-level source file later does NOT require a Dockerfile update.
COPY . /opt/protoagent/
RUN chmod +x /opt/protoagent/entrypoint.sh

# The React console — copied from the node builder stage, NOT from the build
# context (apps/web/dist is .gitignored and node_modules is .dockerignore'd, so
# the source COPY above ships no built console). This is what makes the
# `console` tier serve /app instead of 404'ing (the #874 bug). Only the static
# dist/ lands here — the builder's node_modules stay in the discarded stage.
COPY --from=web-builder /build/apps/web/dist /opt/protoagent/apps/web/dist

# Sandbox workspace + knowledge/audit dirs. /sandbox is the container's instance
# root (PROTOAGENT_HOME=/sandbox, set in entrypoint.sh): live config + secrets +
# setup marker + SOUL.md live at /sandbox/config/*, plugins at /sandbox/plugins,
# every store under /sandbox/ — all persisted by the protoagent-sandbox volume.
RUN mkdir -p /sandbox /tmp/sandbox /sandbox/audit /sandbox/knowledge \
    && chown -R sandbox:sandbox /sandbox /tmp/sandbox

ENV PYTHONPATH=/opt/protoagent
# UI tier baked from the build arg (ADR 0010): the image runs `--ui $UI` (default
# 'none' = API + A2A + /metrics only). server.py reads PROTOAGENT_UI.
ENV PROTOAGENT_UI=${UI}

USER sandbox
WORKDIR /sandbox

EXPOSE 7870

# Readiness/health: /healthz returns 200 only once the agent graph is compiled
# (503 during the model-cold-start window). start-period covers the
# frozen-sidecar / first-compile boot so a slow start isn't marked unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:7870/healthz || exit 1

CMD ["/opt/protoagent/entrypoint.sh"]
