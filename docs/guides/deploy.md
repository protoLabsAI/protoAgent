# Deploy via GHCR

The template ships an autonomous release pipeline. Wire it up once and every merge to `main` produces a fresh container image Watchtower can pick up.

## What the pipeline does

| Trigger | Workflow | Result |
|---|---|---|
| Push to `main` | `docker-publish.yml` | `ghcr.io/<owner>/<image>:latest` + `sha-<short>` |
| Manual (`workflow_dispatch`) | `prepare-release.yml` | Opens a `prepare-release/vX.Y.Z` bump PR (**no auto-merge, no tag** — a human merges it, then pushes the tag) |
| `vX.Y.Z` tag pushed | `release.yml` | Pushes semver Docker tags, creates GitHub release, posts Discord embed |

Rolling `latest` is handled only by `docker-publish.yml`. Stable semver tags are handled only by `release.yml`. The two workflows never collide. The release cadence is **manual** (fleet policy): you dispatch `prepare-release.yml`, merge the bump PR, then push the tag.

## 1. Un-freeze the release pipeline

The release workflows are **opt-in via a repo variable** — there is no
`github.repository ==` guard to swap. Set it on your fork:

```bash
gh variable set RELEASE_ENABLED --body true
```

`prepare-release.yml` and `release.yml` gate on `if: vars.RELEASE_ENABLED == 'true'`. `docker-publish.yml` has no guard — it runs on any push to `main` in any clone.

## 2. Point the image name at your repo

In `release.yml` and `docker-publish.yml`, update `IMAGE_NAME`:

```yaml
env:
  REGISTRY: ghcr.io
  IMAGE_NAME: protolabsai/my-agent   # ← lowercase; GHCR is case-sensitive
```

## 3. Grant `GH_PAT` access

`prepare-release.yml` needs a PAT (not the default `GITHUB_TOKEN`) to push the release branch so its CI checks fire — branches/tags pushed with the default `GITHUB_TOKEN` do not trigger downstream workflows, by GitHub's design.

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
      # REQUIRED for a port published beyond loopback: the server refuses a
      # token-less non-loopback bind (set PROTOAGENT_ALLOW_OPEN=1 instead only
      # if a firewall/network policy fences the port).
      A2A_AUTH_TOKEN: ${A2A_AUTH_TOKEN}
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
> `/metrics`, no console, core deps only (the lean server stack). For the React
> console in the image, build with **`--build-arg UI=console`**; it then runs
> `console`. Setup is headless in `none` — drop a config +
> `OPENAI_API_KEY`, the graph compiles on boot (or run `--setup`); `GET /healthz`
> reports readiness. See [Sandboxing & egress](/guides/sandboxing) and the
> [env-vars reference](/reference/environment-variables#deployment-ui-tier-adr-0010).

## 7. Cut a release

From the Actions tab, run `prepare-release.yml` manually and pick `patch` / `minor` / `major`. It opens a `chore: release vX.Y.Z` bump PR (version + rolled-up CHANGELOG) — **no auto-merge, no auto-tag**. A human merges it once CI is green, then pushes the `vX.Y.Z` tag, which triggers `release.yml` → stable semver Docker tags → GitHub release → Discord post (if configured). See [Releasing](/guides/releasing) for the full flow.

Releases are **manual / on-demand** — merging a PR does **not** cut a release. See the [Releasing runbook](/guides/releasing) for the changelog protocol + the branch ruleset.

## Related

- [Fork the template](/guides/fork-the-template) — the earlier steps that set up the rest
- [Environment variables reference](/reference/environment-variables) — runtime env
