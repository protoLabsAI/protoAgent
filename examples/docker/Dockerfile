# Reference: deploy protoAgent in Docker as config-as-code (seed + UI override).
# Full walkthrough: docs/guides/deploy-docker.md
FROM ghcr.io/protolabsai/protoagent:latest

# Bake a read-only seed outside /sandbox, the persistent instance root.
# First boot copies it to /sandbox/config/langgraph-config.yaml. Later boots
# retain operator edits unless PROTOAGENT_SEED_MERGE opts into per-key merging.
COPY langgraph-config.seed.yaml /opt/agent/seed/langgraph-config.yaml
ENV PROTOAGENT_SEED_CONFIG=/opt/agent/seed/langgraph-config.yaml

# (Add your plugins the same way bundle/fork images do, e.g.
#   RUN git clone --depth 1 https://github.com/you/your-plugin /opt/protoagent/plugins/yours
# and list it under plugins.enabled in the seed.)
