# Back up and restore your data

To recover chats, memory, tasks, settings, and installed plugins, copy protoAgent's
app data while it is stopped. Keep the backup on a separate drive or in a private
backup location. It contains credentials and conversation content.

| What you want to preserve | Use |
| --- | --- |
| Your existing agent and its history | The data backup below |
| A recipe for a fresh agent, or a copy to share | [Settings → Snapshot](/guides/agent-snapshots) |
| Documents and repositories the agent works on | Back up those work folders separately |

## Find your data

For the packaged desktop app, open this folder in your file manager:

| Platform | App data folder | How to open it |
| --- | --- | --- |
| macOS | `~/Library/Application Support/studio.protolabs.protoagent/` | Finder → Go → Go to Folder; paste the path |
| Windows | `%APPDATA%\studio.protolabs.protoagent\` | Paste the path into File Explorer's address bar |
| Linux | `~/.config/studio.protolabs.protoagent/` | Open the path in your file manager; use `$XDG_CONFIG_HOME/studio.protolabs.protoagent/` if you changed that setting |

This folder holds the desktop agent, its fleet workspaces, and shared settings
and credentials. Copy the whole folder, including hidden files. Copying only
`config/` does not preserve your conversations.

For a Python or source installation, run `protoagent config explain` with the
same environment as the server (in a checkout, use
`uv run python -m server config explain`). Back up the reported **box root** and
any **instance root** outside it. A normal host box root is `~/.protoagent/`.
See [Docker backups](#docker-backups) for containers.

Work folders, symlink targets, and explicitly configured stores outside these
roots need their own backups. A backup of app data does not include those files.

## Make a backup

1. Let active work finish, or stop it in chat. Save any documents you have open
   and note the app version from **Settings → Overview**.
2. Quit protoAgent from its tray or menu-bar icon using **Quit**. Closing its
   window can leave the server running. Stop separately launched agents that
   write to the same folders too.
3. Copy the entire app data folder to your backup location. Name the copy with
   the date and the app version you noted.
4. Open the copy and confirm it contains `config/`, including your live config,
   and the stores present in the original. Keep SQLite `-wal` and `-shm` files
   with their databases if they are present.
5. Reopen protoAgent and check that your chats are still available.

A completed folder copy is the backup. Exporting a snapshot or reinstalling the
app does not make one. Avoid copying live databases: concurrent writes can make
a file copy incomplete or inconsistent.

## Restore a backup on the same machine

Restoring returns the agent to the backup date. Later chats, settings, and other
state will be replaced. Restored schedules may fire on startup; review
[missed-fire behavior](/guides/scheduler#missed-fire-recovery) before reopening.

1. Quit protoAgent completely and stop other agents using the folders being restored.
2. Rename the current app data folder to a separate name, such as
   `studio.protolabs.protoagent.before-restore`. Keep it until recovery is verified.
3. Copy the backup into the original location, using the original folder name.
   Restore the complete folder rather than merging individual database files.
4. Start the same app version used to make the backup, if available. Open an
   expected chat, check your knowledge and tasks, and review scheduled work.
5. Send a small test message. If the model asks for a login or rejects a key,
   reconnect in **Settings → Model → Connections**.

If recovery fails, quit again and move the attempted restore aside. Put the
saved pre-restore folder back under the original name. Keep both copies while
investigating the error; see [Troubleshooting](/guides/troubleshooting).

## Move to another machine

For a fresh agent with the same persona and tools, use an
[agent snapshot](/guides/agent-snapshots). It leaves history and credentials behind.

To move history too, keep the original backup and copy the full data folder into
the destination app's data location while both apps are stopped. Local paths,
symlinks, work folders, installed binaries, and subscription logins may need
repair on the destination. Check them before resuming work. Cross-platform raw
restores have not been validated; prefer snapshots for a new platform.

## Docker backups

These commands apply to the [Docker example](/guides/deploy-docker), whose service
is `agent` and whose instance data is mounted at `/sandbox`. Run them on the
Docker host, from the Compose project directory.

First find the **actual volume name** mounted at `/sandbox`:

```bash
docker inspect "$(docker compose ps -aq agent)" \
  --format '{{range .Mounts}}{{if eq .Destination "/sandbox"}}{{.Name}}{{end}}{{end}}'
```

Set the name printed above, stop the service, and archive the volume through a
helper container:

```bash
export PROTOAGENT_DATA_VOLUME=your-project_agent-sandbox
docker compose stop agent
docker run --rm \
  --mount "type=volume,src=$PROTOAGENT_DATA_VOLUME,dst=/data,readonly" \
  alpine:3.22 tar -czf - -C /data . > protoagent-data.tgz
docker compose start agent
```

Check that the archive lists successfully with `tar -tzf protoagent-data.tgz`.
The archive contains everything in the volume, including secrets. If you use a
bind mount instead, stop the service and copy its host directory.

To restore, keep the current volume and extract into a **new** volume:

```bash
docker compose stop agent
docker volume create protoagent-restore
docker run --rm -i \
  --mount type=volume,src=protoagent-restore,dst=/data \
  alpine:3.22 tar -xzf - -C /data < protoagent-data.tgz
```

Point the Compose service's `/sandbox` mount at this restored volume. For the
example, keep `agent-sandbox:/sandbox` and set its top-level volume declaration to:

```yaml
volumes:
  agent-sandbox:
    external: true
    name: protoagent-restore
```

Run `docker compose up -d`, then verify the console and data as above. Keep the
original volume until verification passes. Restored schedules can run on startup;
review the [missed-fire behavior](/guides/scheduler#missed-fire-recovery) beforehand.
