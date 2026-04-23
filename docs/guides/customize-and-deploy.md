# Customize & deploy

Use this guide when you've run through the wizard, decided the template fits your use case, and now want to fork it into your own GitHub repo + ship a deployable image. If you're still evaluating, stay on the [first-agent tutorial](/tutorials/first-agent) — you don't need any of this to run the agent locally.

## Why this is a separate step

The [setup wizard](/tutorials/first-agent) handles runtime customization — model, tools, persona, auth — without editing code. Everything below is structural: renaming the template throughout the codebase, bending the release pipeline to your repo, baking your fork's identity into the Docker image. Do it once per fork, not every time you tweak a setting.

## 1. Fork the template on GitHub

```bash
gh repo create protoLabsAI/my-agent \
    --template protoLabsAI/protoAgent \
    --public --clone

cd my-agent
```

Or: `Use this template → Create a new repository` from the browser. Pick a short slug (`jon`, `echo-agent`, `product-director`) — it ends up as the image name, metric prefix, Langfuse tag, and release-workflow repo guard.

## 2. Rename `protoagent` throughout

The template uses `protoagent` as the placeholder everywhere. Do one pass:

```bash
# macOS / BSD sed
git grep -li protoagent | xargs sed -i '' 's/protoagent/my-agent/g'
git grep -li protoAgent | xargs sed -i '' 's/protoAgent/MyAgent/g'

# Linux / GNU sed — drop the empty-string backup suffix
git grep -li protoagent | xargs sed -i 's/protoagent/my-agent/g'
git grep -li protoAgent | xargs sed -i 's/protoAgent/MyAgent/g'
```

Review the diff. Key hits:

- `Dockerfile` — the `/opt/protoagent/` paths become `/opt/my-agent/`.
- `entrypoint.sh` — same.
- `server.py` — `AGENT_NAME_ENV` fallback becomes `my-agent`.
- `chat_ui.py` — branding strings (service worker label, apple-mobile-web-app-title).
- Workflow files — the repo guards check `protoLabsAI/my-agent` instead.

The runtime name (`identity.name` in `config/langgraph-config.yaml`, set by the wizard) is separate — keep both in sync unless you have a reason not to.

## 3. Un-freeze the release pipeline

The release workflows gate on the template's repo path so third-party clones don't accidentally cut releases:

- `.github/workflows/prepare-release.yml`
- `.github/workflows/release.yml`
- `.github/workflows/docker-publish.yml`

Each has a `if: github.repository == 'protoLabsAI/protoAgent'` (or similar) check. Swap `protoLabsAI/protoAgent` for `<your-org>/<your-repo>` in all three, or the pipeline won't fire on merges.

## 4. Rewrite the agent card

`server.py::_build_agent_card` ships with placeholder skills:

```python
"skills": [
    {"id": "chat", "name": "Chat", "description": "General-purpose...", ...},
],
```

Replace with the skills your agent actually advertises over A2A. The `name` and `url` fields already pick up `identity.name` from YAML, so the wizard-set name lands on the card without code changes.

## 5. (Optional) Add domain tools

`tools/lg_tools.py` ships with `echo`, `current_time`, `calculator`, `web_search`, `fetch_url`. Keep the ones you want, drop the rest, add your own. Update `get_all_tools()` at the bottom. Any tool returned from there becomes a checkbox in the wizard and drawer automatically.

## 6. (Optional) Configure subagents

`graph/subagents/config.py` ships with one `worker`. Register more `SubagentConfig` instances in `SUBAGENT_REGISTRY` and add matching fields in `graph/config.py::LangGraphConfig`. The lead agent delegates via the `task` tool; the subagent delegation rules are built from the registry.

## 7. Build and ship the image

```bash
docker build -t ghcr.io/my-org/my-agent:local .

# local test — mount the config volume so wizard completions persist
docker run --rm -p 7870:7870 \
    -e OPENAI_API_KEY="$OPENAI_API_KEY" \
    -v my-agent-config:/opt/my-agent/config \
    ghcr.io/my-org/my-agent:local
```

The Dockerfile declares `VOLUME /opt/<agent>/config` so even without `-v` the wizard writes persist across container runs on the same Docker host — they live in an anonymous volume. For production, use a named volume or host mount so you can back it up.

Once the local build is happy, merge a PR to trigger the release pipeline ([Deploy via GHCR](/guides/deploy)).

## 8. Delete `TEMPLATE.md`

Once the checklist is done, `rm TEMPLATE.md` and rewrite `README.md` to describe your specific agent — its purpose, its skills, its operators.

## Canonical reference implementation

[protoLabsAI/quinn](https://github.com/protoLabsAI/quinn) is the first agent built on this template, now running in production. When this guide doesn't cover a specific decision, Quinn is the filled-in example — worth a skim before you invent something new.
