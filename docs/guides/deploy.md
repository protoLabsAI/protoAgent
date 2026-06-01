# Deploy via GHCR

The template ships an autonomous release pipeline. Wire it up once and every merge to `main` produces a fresh container image Watchtower can pick up.

## What the pipeline does

| Trigger | Workflow | Result |
|---|---|---|
| Push to `main` | `docker-publish.yml` | `ghcr.io/<owner>/<image>:latest` + `sha-<short>` |
| Non-release PR merged | `prepare-release.yml` | Opens `prepare-release/vX.Y.Z` bump PR, auto-merges, tags `vX.Y.Z` |
| `vX.Y.Z` tag pushed | `release.yml` | Pushes semver Docker tags, creates GitHub release, posts Discord embed |

Rolling `latest` is handled only by `docker-publish.yml`. Stable semver tags are handled only by `release.yml`. The two workflows never collide.

## 1. Un-freeze the repo guards

Three files gate on `github.repository == 'protoLabsAI/protoAgent'`. Update them to your fork's path:

- `.github/workflows/prepare-release.yml`
- `.github/workflows/release.yml`

The `docker-publish.yml` workflow doesn't have this guard — it runs on any push to `main` in any clone.

## 2. Point the image name at your repo

In all three workflow files, update `IMAGE_NAME`:

```yaml
env:
  REGISTRY: ghcr.io
  IMAGE_NAME: protolabsai/my-agent   # ← lowercase; GHCR is case-sensitive
```

## 3. Grant `GH_PAT` access

`prepare-release.yml` needs a PAT (not the default `GITHUB_TOKEN`) to push tags that trigger downstream workflows — `GITHUB_TOKEN`-pushed tags do not fire `on: push: tags` handlers, by GitHub's design.

Create a fine-grained PAT with `contents: write` on the repo, then add it as a secret named `GH_PAT` in **Settings → Secrets → Actions**.

## 4. (Optional) Discord release embeds

`release.yml` delegates to the shared [`protoLabsAI/release-tools`](https://github.com/protoLabsAI/release-tools) Action, which reads two CI secrets:

- `GATEWAY_API_KEY` — bearer token for the protoLabs LLM gateway; the Action rewrites raw commits into themed notes.
- `DISCORD_RELEASE_WEBHOOK` — Discord channel webhook URL. Without it, set `post-discord: false` (notes generate but aren't posted).

The embed footer/repo link can be customized via the Action's `footer` and `repo` inputs — see the [release-tools README](https://github.com/protoLabsAI/release-tools#inputs).

## 5. Verify the first push

Merge any PR to `main` (or push a trivial commit). `docker-publish.yml` should produce:

```
ghcr.io/<owner>/<image>:latest
ghcr.io/<owner>/<image>:sha-<short>
```

Check **Actions** on the repo and **Packages** on the org for the image.

## 6. Point Watchtower at `latest`

On your deploy host (or wherever your compose / IaC lives):

```yaml
services:
  my-agent:
    image: ghcr.io/protolabsai/my-agent:latest
    restart: unless-stopped
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
    environment:
      AGENT_NAME: my-agent
      OPENAI_API_KEY: ${LITELLM_MASTER_KEY}
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
    ports:
      - "7870:7870"
    volumes:
      - audit:/sandbox/audit
      - knowledge:/sandbox/knowledge

  watchtower:
    image: containrrr/watchtower
    command: --interval 60 --label-enable
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

Watchtower polls `latest` every 60 seconds and recreates the container when the image hash changes.

> **UI tier (ADR 0010):** the image defaults to **`--ui none`** — API + A2A +
> `/metrics`, no console, no Gradio, core deps only (the lean server stack). For
> the Gradio UI in the image, build with **`--build-arg UI=full`** (adds
> `gradio`); it then runs `full`. Setup is headless in `none` — drop a config +
> `OPENAI_API_KEY`, the graph compiles on boot (or run `--setup`); `GET /healthz`
> reports readiness. See [Sandboxing & egress](/guides/sandboxing) and the
> [env-vars reference](/reference/environment-variables#deployment-ui-tier-adr-0010).

## 7. Cut a release

From the Actions tab, run `prepare-release.yml` manually and pick `patch` / `minor` / `major`. It opens a bump PR, auto-merges it once the required checks pass, and tags `vX.Y.Z`, which triggers `release.yml` → stable semver Docker tags → GitHub release → Discord post (if configured).

Releases are **manual / on-demand** — merging a PR does **not** cut a release. See the [Releasing runbook](/guides/releasing) for the changelog protocol + the branch ruleset.

## Related

- [Fork the template](/guides/fork-the-template) — the earlier steps that set up the rest
- [Environment variables reference](/reference/environment-variables) — runtime env
