# Reference: deploy protoAgent in Docker as config-as-code (seed + UI override).
# Full walkthrough: docs/guides/deploy-docker.md
FROM ghcr.io/protolabsai/protoagent:latest

# Bake your agent's config as a SEED — NOT as the live /opt/protoagent/config/
# langgraph-config.yaml. That path is a VOLUME; baking the live file there means a
# config volume freezes your first-boot config and silently shadows every later
# image update. Instead point PROTOAGENT_SEED_CONFIG at a plain (non-volume) path:
# protoAgent's first-boot seeder copies it to the live config once, then leaves it
# alone, so console edits persist on the volume and image rebuilds still re-seed a
# FRESH instance.
COPY langgraph-config.seed.yaml /opt/agent/seed/langgraph-config.yaml
ENV PROTOAGENT_SEED_CONFIG=/opt/agent/seed/langgraph-config.yaml

# (Add your plugins the same way bundle/fork images do, e.g.
#   RUN git clone --depth 1 https://github.com/you/your-plugin /opt/protoagent/plugins/yours
# and list it under plugins.enabled in the seed.)
