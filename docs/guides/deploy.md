# Publish images to GHCR

<span id="deploy-via-ghcr"></span>

Publish your fork's container image, then deploy it using the
[Docker guide](/guides/deploy-docker). You need GitHub Actions enabled on the fork
and permission to publish its GHCR package. The published image is multi-arch
(`linux/amd64` + `linux/arm64`).

## Wire it up (once)

Run these from your fork's checkout, replacing the image name with your lowercase
owner and package name:

```bash
gh variable set IMAGE_NAME --body your-owner/your-agent
gh variable set DOCKER_PUBLISH_ENABLED --body true
gh workflow run docker-publish.yml
```

The canonical repository publishes automatically; **forks must opt in** with
`DOCKER_PUBLISH_ENABLED=true`. Both image workflows read the `IMAGE_NAME`
repository variable; without it they default to `protolabsai/protoagent`.

## What the pipeline does

| Trigger | Workflow | Result |
| --- | --- | --- |
| Push to `main` or manual dispatch | `docker-publish.yml` | `latest` and `sha-<short>` images |
| Manual release preparation | `prepare-release.yml` | Version/changelog PR; no automatic merge or tag |
| `vX.Y.Z` tag or manual release dispatch | `release.yml` | `X.Y.Z` and `X.Y` images, GitHub release |

Release preparation and publication separately require `RELEASE_ENABLED=true`.
The [Releasing runbook](/guides/releasing) owns that procedure and its secrets.

<span id="_1-un-freeze-the-release-pipeline"></span>
<span id="_2-point-the-image-name-at-your-repo"></span>
<span id="_3-grant-gh-pat-access"></span>
<span id="_4-optional-discord-release-embeds"></span>
<span id="_7-cut-a-release"></span>

## Versioned releases

Enable `RELEASE_ENABLED` when you want versioned releases in addition to rolling
images. Follow [Releasing](/guides/releasing) to configure `GH_PAT`, prepare and
merge the release PR, and push its tag. That guide also covers optional Discord
notifications. Ordinary merges do not create versioned releases.

## Verify the first push {#_5-verify-the-first-push}

Check **Actions → Publish Docker image** and your account's **Packages** for:

```text
ghcr.io/your-owner/your-agent:latest
ghcr.io/your-owner/your-agent:sha-<short>
```

Use the [Docker deployment example](/guides/deploy-docker) with your image name.
Console assets are already bundled: set `PROTOAGENT_UI=console` to serve `/app`;
the image defaults to `none`. A frontend rebuild is not required to change tiers.

## Choose an update policy {#_6-point-watchtower-at-latest}

`latest` follows successful main-branch builds. Use a commit tag, full version
or digest when you need to control deployment timing. Record the running image
and [back up its data](/guides/backup-and-restore#docker-backups) before upgrading.
See [Docker day-2 operations](/guides/deploy-docker#day-2) for pull versus rebuild.

An automatic image updater such as Watchtower also restarts the agent during
active work. Use one only if that interruption is acceptable. An older image
alone is not a complete rollback: keep the matching pre-upgrade data backup.

## Operations

### Backup & restore

Persist the entire `/sandbox` volume. Follow
[Docker backups](/guides/backup-and-restore#docker-backups) to stop the agent,
archive the volume, and restore into a separate volume. `/sandbox` is inside the
container; it is not a host directory unless you bind-mounted one there.

### Shutdown semantics

On `SIGTERM` or `SIGINT`, uvicorn stops accepting connections and allows up to
**five seconds** for in-flight requests to drain, then cancels remaining request
tasks. Application shutdown hooks subsequently stop services such as fleet
members and the scheduler. Long-lived streams can be disconnected.

Increasing Docker's stop timeout gives shutdown hooks more time before
`SIGKILL`; it **does not extend the server's five-second request drain**. Let
important turns finish before updating. After an interrupted turn, inspect the
durable task and any tool side effects before resubmitting; shutdown is not a
transaction rollback or a promise that work resumes automatically.

## Related

- [Fork the template](/guides/fork-the-template)
- [Environment variables](/reference/environment-variables)
