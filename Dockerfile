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
# image) or 'console' (also serves the React console, which ships as COPY'd static
# assets, not a pip dep). Either tier uses the same lean core deps, so the install
# is unconditional; forks that need extras (the google/Discord MCP surfaces,
# agent-browser, …) add them to requirements-core.txt (note above).
ARG UI=none
COPY requirements*.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-core.txt

# Single COPY with a matching .dockerignore covers everything that
# should ship and excludes .git/, tests/, docs, and dev state. Adding a
# new top-level source file later does NOT require a Dockerfile update.
COPY . /opt/protoagent/
RUN chmod +x /opt/protoagent/entrypoint.sh

# Sandbox workspace + knowledge/audit dirs
RUN mkdir -p /sandbox /tmp/sandbox /sandbox/audit /sandbox/knowledge \
    && chown -R sandbox:sandbox /sandbox /tmp/sandbox

# Make /opt/protoagent/config writable by the sandbox user so the
# drawer and setup wizard can persist edits from inside the container.
RUN chown -R sandbox:sandbox /opt/protoagent/config

# Declare config as a volume so setup completion (``.setup-complete``
# marker + any YAML / SOUL.md edits) survives ``docker run`` without
# a -v flag.
#
# Lifecycle note: without an explicit mount, Docker creates an
# ANONYMOUS volume on every ``docker run``. Those accumulate and the
# volume is NOT removed when the container is removed unless you pass
# ``--rm -v``. For long-lived deployments, use a named volume or a
# host mount so upgrades don't silently carry stale config forward:
#
#   docker run -v my-agent-config:/opt/protoagent/config my-agent:latest
#
# or a bind mount:
#
#   docker run -v /srv/my-agent/config:/opt/protoagent/config my-agent:latest
VOLUME ["/opt/protoagent/config"]

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
