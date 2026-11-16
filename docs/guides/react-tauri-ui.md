# Use the app

Use protoAgent to chat with an agent, inspect its work, and change its settings.
The desktop app and browser console share the same controls. For installation,
follow [Set up your first agent](/tutorials/first-agent).

## Run it

Open the desktop app, or use the launch command for your
[installation route](/tutorials/first-agent#_1-install-and-start). The Python-package
walkthrough uses `uvx`; it does not require a globally installed `protoagent` command.

## Layout

1. **Agent switcher** (top left): choose the agent you are working with.
2. **Left rail**: open Chat, Knowledge, Memory, Docs, and installed plugin views.
3. **Chat workspace**: switch conversation tabs, read tool results, and send messages.
4. **Right rail**: open [Work](#work) — the agent's jobs, goals, follow-ups, and
   standing instructions, grouped by what needs your attention — or inspect
   artifacts and notes beside the conversation.

**Settings**, **Search commands**, and **Feed** are in the bottom-left toolbar.
Feed combines pending inbox items and completed agent-initiated work.

Press **⌘⇧K** / **Ctrl-Shift-K** for the
[command palette](/guides/command-palette), or click the search icon, to jump to a
surface or Settings section. In a [fleet](/guides/fleet), switch agents or open
separate windows to work with more than one at once.

<!-- docs-media: app-layout -->
<figure class="docs-media"><img src="/media/app-layout/v1.png" alt="Four numbered callouts mark the agent switcher, left navigation rail, chat workspace, and right work rail." loading="lazy" style="max-width:100%;height:auto"><figcaption>The numbered callouts match the four layout areas described above.</figcaption></figure>
<!-- /docs-media -->

## Chat

1. Use **+** in the chat tab bar to start a conversation. Write a message and send it.
2. Return to a conversation by selecting its tab or searching its title in the
   [command palette](/guides/command-palette). Switching tabs does not stop its work. With focus in chat (including the composer),
   use **⌘[** / **⌘]** on macOS or **Ctrl+[** / **Ctrl+]** elsewhere for the previous / next
   tab, wrapping at either end. Rebind these in **Settings ▸ Keyboard**.
3. To change direction while it works, send a follow-up message. To stop the
   current turn, use the composer’s stop control.

Stopping does not undo files or external actions already completed. Inspect the
last tool result before retrying a task. If a question or approval form is open,
answer or dismiss that form first; follow-up messages wait until it resumes.
See [Steering](/explanation/steering) for pending-message behavior.

- **Tool-call cards** show what the agent called, whether it is running or finished,
  and its input and result. Expand a card to inspect it.
- **Slash commands:** type `/` to see available commands and installed skills.
- **Steering:** send another message while the agent works to give it a correction
  or more context. It reads that message at the next model call.
- **Model:** choose one for this chat; see [Connect and change models](/guides/model-connections).
- **Delegation:** a [delegate](/guides/delegates) can return a reply under its own
  name.
- **Status rail:** a strip above the composer sums up this chat's work — a **Needs
  you** marker and a chip for each non-empty [Work](#work) group (**Doing now**,
  **Coming up**, **Instructions**). A chip opens that group's rows; **Needs you**
  is never limited to one chat.

On desktop, a failed streaming relay can fall back to showing the completed reply.
If a reply stalls or fails, follow [Chat fails](/guides/troubleshooting#chat-fails).

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

Open **Feed** from the bottom-left activity icon. It is one newest-first list,
grouped by day: a pending inbound message shows as a quiet **Queued** row, a
completed agent-initiated turn (including scheduled tasks) as a row showing the
result and what triggered it, and a failed turn carries a **Failed** badge. The
badge on the icon counts unread items.

Inbox messages can come from webhooks, scripts, or other agents. A `now` item
starts a turn immediately; `next` and `later` items wait for the agent to check
them. Nothing in the Feed asks for action: a `now` item whose immediate delivery
failed is retried automatically when the agent is free, and a page that genuinely
needs you — operator-class, or out of automatic retries — appears in
[**Work › Needs you**](#work), where its Delivery detail lets you dismiss it.

## Work

Open **Work** from the right rail to see everything the agent is carrying. It
sorts that work into four groups, with an **Overview** that lists them together:

- **Needs you** — decisions and jobs waiting on your answer, goals that stopped
  (out of steps, or judged unachievable), and pages the agent couldn't deliver.
- **Doing now** — what the agent is actively working: a running goal, jobs in
  progress, and running background work.
- **Coming up** — work that hasn't started yet: not-started jobs and follow-ups,
  the one concept that now covers a time trigger, a watched condition, and a wait.
- **Your instructions** — standing instructions you've given that are still in force.

Opening a group from the chat [status rail](#chat) scopes that visit to the
current conversation. Choosing another section in Work ends that scope.
**Needs you** is the exception: it always counts everything waiting on you,
across the agent's chats.

### Answer a job that's waiting on you

A job parked on your decision sits in **Needs you**. Open it to read the agent's
question and any links it left, then act in place:

- **Reply** — send your answer. If the job's source conversation has no turns to
  reply into, this starts a new chat titled `Re: <the job's title>`; otherwise
  your reply continues the original chat.
- **Done…** / **Dismiss…** — close the job out, with an optional reason. The
  agent is told the outcome and your reason.

### Where a job started, and when it stalls

Jobs retain their source conversation. For jobs that aren't waiting on you,
**Started in** links to the source chat or thread when available; background
sources read "Started by a background job". A waiting job offers **Open chat**
when its source has turns; otherwise its **Source** field shows the available
origin, or "Its chat was deleted" for a missing chat. Finished jobs leave Work.

Under a job in **Doing now**, a muted line tells you whether it is really moving.
`in progress · idle 5m` (the time since it was last updated, shown in minutes, or
hours for a longer gap) marks an in-progress job that nothing is currently driving;
`stalled` means it has been left that way past the stall threshold of about
half an hour. Both are quiet notes, not warnings.

When an in-progress job goes more than half an hour without an update and
nothing is driving it, the agent is reminded to pick it back up, up to three
times. Unresolved links, pending scheduled work, active watches, running
delegations, background jobs, and live turns can all keep a job from being
considered stalled. If those reminders go unheeded, the job moves to
**Needs you** for you to continue, defer, or close it.

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
Jobs also retain their source conversation; [Work](#work) provides source links
when that conversation is available.

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

## Protect your data and resolve problems

[Back up your app data](/guides/backup-and-restore) to preserve chats, memory,
settings, and credentials. Use **Settings → Snapshot** when you want a recipe
for a fresh agent instead; it does not restore your history.

If chat, a plugin, or a document task fails, start with
[Troubleshooting](/guides/troubleshooting). It points from the error to the
relevant setting and explains what to check before resetting anything.

<span id="testing-the-console"></span>

For developer setup and testing, see [Build and test the console](/guides/build-console)
and the [operator API reference](/reference/operator-api).
