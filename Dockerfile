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

# Python deps for the base runtime. If your fork needs agent-browser,
# sqlite-vec for a knowledge store, or pyjwt[crypto] for GitHub App
# auth, add them here. The ddgs + beautifulsoup4 pair powers the
# starter web_search / fetch_url tools; drop them if you strip those.
RUN pip install --no-cache-dir \
    gradio httpx uvicorn langfuse prometheus-client pyyaml \
    langchain langchain-openai langgraph websockets \
    ddgs beautifulsoup4

# Single COPY with a matching .dockerignore covers everything that
# should ship and excludes .git/, tests/, docs, and dev state. Adding a
# new top-level source file later does NOT require a Dockerfile update.
COPY . /opt/protoagent/
RUN chmod +x /opt/protoagent/entrypoint.sh

# Sandbox workspace + knowledge/audit dirs
RUN mkdir -p /sandbox /tmp/sandbox /sandbox/audit /sandbox/knowledge \
    && chown -R sandbox:sandbox /sandbox /tmp/sandbox

ENV PYTHONPATH=/opt/protoagent

USER sandbox
WORKDIR /sandbox

EXPOSE 7870
CMD ["/opt/protoagent/entrypoint.sh"]
