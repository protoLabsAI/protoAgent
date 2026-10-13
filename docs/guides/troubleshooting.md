# Fix a problem in the app

Start with the symptom below. Keep the error text and the app version from
**Settings → Overview**. If the app cannot open, use the installed version or
installer filename instead.

| Symptom | Start here |
| --- | --- |
| Blank window or server connection error | [App won't open](#app-won-t-open) |
| Model error or no reply | [Chat fails](#chat-fails) |
| Python runtime missing or document creation fails | [Documents fail](#documents-fail) |
| Plugin missing, disabled, or failing | [Plugin fails](#plugin-fails) |
| File access is refused | [File access fails](#file-access-fails) |
| Expected chats or settings are missing | [Data appears missing](#data-appears-missing) |

## App won't open

Quit from the tray or menu-bar icon using **Quit**, then reopen the app. Closing
its window can leave it running. If an alert says **protoAgent server problem**,
read its error and the log location shown there.

The desktop app chooses a free local port on launch. A browser at
`http://localhost:7870` can therefore reach another instance or no instance at all.
Use the desktop window to access its selected server; do not reset data to fix a
port conflict.

On Windows, follow the [install and recovery guide](/guides/windows-desktop#_7-safe-recovery)
if a clean restart does not help. Before any reset or manual data change,
[make a data backup](/guides/backup-and-restore).

## Chat fails

1. Read the error on the message or expanded tool-call card. A failed tool and a
   failed model connection need different fixes.
2. Open **Settings → Model → Connections**. Select the connection used by your
   model and choose **Test**. Check the endpoint, selected model, and key;
   for a local endpoint, make sure its server is running.
3. If a Claude or ChatGPT subscription is signed out or expired, use its sign-in
   action. Adding an API key to another connection will not repair that login.
4. Once the connection test passes, send a short message in a new chat. If it
   succeeds there, keep the original chat and inspect its last failing tool call.

The connection row’s **Test** fetches its model list; a successful list does not
verify that the selected model can answer. See
[Connect and change models](/guides/model-connections) for sign-in, model
selection, and key replacement.

For a stuck turn, use the composer's stop control before retrying. Stopping a
turn does not undo files or external actions already completed. If you lost the
connection, reopen the chat and inspect its state before sending the same task again.

## Documents fail

If the error says Python is missing, open **Settings → Tools** and complete the
runtime installation. If it says the document libraries are stale, use
**Update runtime**. Wait for completion before retrying.

If installation fails, keep the error text and check that the download can reach
the network and that your disk has space. An unsupported platform notice requires
a supported build; repeated installation attempts will not fix it. Follow
[Enable document creation](/guides/python-runtime) for the install controls.

If execution succeeded but saving failed, check the requested output directory's
[work-folder permissions](#file-access-fails). Try one small document to verify
recovery before repeating the larger task. For a missing download or a file that
was created without an artifact card, follow
[Work with files and documents](/guides/documents-and-files#recover-from-a-failed-document-task).

## Plugin fails

Open **Settings → Plugins → Installed** and read the plugin's status before
reinstalling it. **Attention** narrows the list to errors, missing setup, and
updates; use **All** or **Disabled** to find an inactive plugin:

- **Disabled:** enable it if you want its capabilities available.
- **Missing packages:** choose **Install deps** and inspect the result. Desktop
  may require its managed Python runtime first.
- **Unfinished setup:** open **Configure** and supply the required settings or login.
- **Requires a newer protoAgent:** update the app before enabling the plugin.
- **Missing on disk:** use **Sync plugins** to restore the copies recorded in the lockfile.

If the problem began after enabling a plugin, disable that plugin and retry a
small task. Keep the load error for its author. See
[Install and manage plugins](/guides/plugin-registry) for updates and removal.

## File access fails

Open **Settings → Tools → Filesystem → Work folders**. Add the folder containing
the file, and enable writing for that root if the task changes it. Keep access
limited to the folders the task needs.

The folder must exist on the machine running the agent. A remote agent cannot
read a path from your laptop. A symlink pointing outside the allowed roots is
also refused; add its target root if that access is intended.

Retry with the exact file path. See [Use the app](/guides/react-tauri-ui#working-memory-the-filesystem-fence)
for the file-tool rules. CLI coding delegates have their own tools and permission
settings; see [Coding agents](/guides/coding-agents#permission-posture).

## Data appears missing

Check which agent is selected and which installation you opened. The desktop app,
a source checkout, and a Docker deployment normally use separate data locations.
A new setup wizard or empty history can mean you opened a different instance.

Do not finish a new setup or overwrite folders until you locate the old data.
Use the [data-location table](/guides/backup-and-restore#find-your-data), and
`config explain` for a source or Python installation. If you have a backup,
follow [Restore a backup](/guides/backup-and-restore#restore-a-backup-on-the-same-machine).
An agent snapshot creates a fresh agent and cannot restore chat history.

## Report a problem

Include the app version, operating system, desktop or browser surface, the steps
to reproduce it, expected behavior, and exact error text. Attach only the relevant
log excerpt, with tokens, keys, and private content removed. The desktop's startup
alert gives the log location; a source or package server writes to its terminal.

Use the console's [`/issue` command](/guides/file-github-issues) if available, or
[open a GitHub issue](https://github.com/protoLabsAI/protoAgent/issues/new/choose).
Do not attach a raw data backup: it includes your conversations and credentials.
