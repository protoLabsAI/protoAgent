FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential gettext-base gnupg \
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

# Non-root sandbox user
ARG SANDBOX_UID=1001
RUN useradd -m -s /bin/bash -u ${SANDBOX_UID} sandbox

# Python deps — installed from requirements.txt so the runtime image stays
# in lockstep with local + CI installs. A hand-maintained list here drifts
# (it had silently lost `croniter`, which the scheduler imports). Copy just
# the requirements first so this layer stays cached across source-only
# changes. Forks that need extras (agent-browser, sqlite-vec, pyjwt[crypto])
# add them to requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

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

USER sandbox
WORKDIR /sandbox

EXPOSE 7870
CMD ["/opt/protoagent/entrypoint.sh"]
