# Use the app

Use protoAgent to chat with an agent, inspect its work, and change its settings.
The desktop app and browser console share the same controls. For installation,
follow [Set up your first agent](/tutorials/first-agent).

## Run it

Open the desktop app, or start an installed Python package with `protoagent serve`
and visit <http://localhost:7870>. For a source checkout, build the frontend first:
[Build and test the console](/guides/build-console).

## Layout

Use the rail to open **Chat**, **Activity**, **Knowledge**, and **Settings**.
Enabled plugins add their own views. Goals, tasks, schedules, and notes provide
views of the agent's ongoing work.

Press **⌘⇧K** / **Ctrl-Shift-K** for the
[command palette](/guides/command-palette), or click the search icon, to jump to a
surface or Settings section. In a [fleet](/guides/fleet), switch agents or open
separate windows to work with more than one at once.

## Chat

Open a chat, write a message, and send it. Each conversation has its own history
and status. Switching tabs lets another conversation continue working.

- **Tool-call cards** show what the agent called, whether it is running or finished,
  and its input and result. Expand a card to inspect it.
- **Slash commands:** type `/` to see available commands and installed skills.
- **Steering:** send another message while the agent works to give it a correction
  or more context. It reads that message at the next model call.
- **Model:** choose one for this chat; see [Connect and change models](/guides/model-connections).
- **Delegation:** a [delegate](/guides/delegates) can return a reply under its own
  name; background work appears above the composer.

The browser streams replies directly. Desktop streams through the native shell;
if that relay fails, it falls back to displaying the completed reply.

## Save a conversation

Run `/export` to download the current chat as Markdown. Check the status note
and your downloads folder. Read the file before sharing: recognizable secret
patterns are redacted, but private content and unrecognized secrets can remain.
This export is a readable record; restoring the app's history needs a
[data backup](/guides/backup-and-restore).

To empty or delete a chat and choose what saved memory to remove, follow
[Manage memory](/guides/manage-memory#delete-a-chat).

## Create and download documents

[Work with documents and files](/guides/documents-and-files) covers attaching
sources, choosing a save folder, creating office documents, downloading results,
and asking for revisions. File artifacts are available from their chat chips
or the **Artifact** panel.

## Activity and inbox {#reactive-surfaces-adr-0003}

**Activity** holds agent-initiated work, including scheduled tasks. Open it to
read results or reply in the Activity conversation. The unread badge counts new
items while you are elsewhere; the live dot shows the event connection.

**Inbox** holds messages from webhooks, scripts, and other agents. A `now` item
starts an Activity turn immediately; `next` and `later` items wait for the agent
to check them. Read or dismiss items here.

## Change settings {#agent-settings-telemetry}

Open **Settings** to change the focused agent:

| Section | Use it to |
| --- | --- |
| **Identity** | Change its name and persona (`SOUL.md`) |
| **Model → Connections** | Add or test model endpoints and subscription logins |
| **Tools**, **MCP**, **Plugins** | Control tools and integrations |
| **Skills**, **Subagents**, **Delegates** | Manage procedures and agents it can call |
| **Knowledge** | Tune recall and memory |
| **Operator & access**, **Devices** | Configure access and pairing |
| **Telemetry** | Inspect local usage, cost, and latency |

Most changes apply on save. A setting that needs a restart carries a restart
badge. Secret inputs show whether a value is set; they do not display it again.

## Inspect and control memory

Open **Memory** to read past-session summaries, edit hot memory, or inspect what
was injected into a turn. **Knowledge → Store** searches saved facts and documents
and offers review, edit, and delete controls. Follow [Manage memory](/guides/manage-memory)
for those tasks, including incognito and deleting a chat's saved content.

## Working memory & the filesystem fence

The focused agent's knowledge, tasks, notes, and memory are shared across its
chats. They belong to the agent instance, rather than to a selected project.

In **Settings → Tools → Filesystem → Work folders**, add the directories the
agent can access. Each root has its own write permission. File tools resolve
paths and symlinks against these roots and reject paths outside them. With no
explicit folders, they use the instance's default `workspace` directory.

**Browse…** lists directories on the **server's machine**. When configuring a
remote agent, choose paths that exist there. The `operator.allowed_dirs` and
`operator.project_dir` config fields do not grant file access.

## The code pane

Enable **Settings → Tools → Filesystem → Shell & filesystem tools** and
**Code pane**, then **Save & apply**. Both switches must be on.

Open a file from a path link in chat or an agent's code chip. **File** shows
its contents and highlighted lines; **Diff** shows working-tree changes against
the latest commit. Use **Recent** to return to a file you opened earlier.

On desktop, **Follow** moves the pane to files the agent uses. **Pin** holds
it in place while you read. On a phone, open code chips yourself. Files use
the same work-folder access rules as file tools. Hidden, missing, binary, or
oversized files show a notice rather than their normal preview.

## Open files in your editor

In **Settings → Chat → Open files in**, choose **protoAgent**, **Zed**,
**VS Code**, **Cursor**, or **Off**. **protoAgent** opens the code pane and is
available while that pane's toolset is enabled. With it selected,
**External editor** chooses where ⌘/Ctrl-click and the pane's ↗ button open a file.
With an external editor selected, ⌘/Ctrl-click opens the pane instead.

These choices belong to this browser or console and stay put when you switch
agents. External-editor links need a shared filesystem; for a remote agent,
use the code pane to read files on its machine.

## Desktop controls {#desktop-app-tauri}

The desktop app starts its bundled server. Closing the main window hides it;
use **Quit** from the tray menu to exit. The tray also offers **Check for Updates…**.
An available update opens release notes and an **Update & Restart** action.

To create office documents, install the
[managed Python runtime](/guides/python-runtime) from **Settings → Tools**.
For Windows installation and recovery, see [Windows desktop](/guides/windows-desktop).

## Testing the console

Developer build, packaging, API, and testing details are in
[Build and test the console](/guides/build-console) and the
[operator API reference](/reference/operator-api).

## Protect your data and resolve problems

[Back up your app data](/guides/backup-and-restore) to preserve chats, memory,
settings, and credentials. Use **Settings → Snapshot** when you want a recipe
for a fresh agent instead; it does not restore your history.

If chat, a plugin, or a document task fails, start with
[Troubleshooting](/guides/troubleshooting). It points from the error to the
relevant setting and explains what to check before resetting anything.
