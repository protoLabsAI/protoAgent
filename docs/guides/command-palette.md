# Command palette (⌘⇧K)

Press **⌘⇧K** on macOS or **Ctrl-Shift-K** on Windows/Linux, or click the
search icon in the utility bar. On a phone, the icon is beside **+** in the
header. Type what you want to open or do, then choose a result.

Try `Model` to change connections, `Memory` to inspect recalled context, or a
chat title to return to that conversation. Press Escape to dismiss the palette.

**⌘K** clears the conversation in chat. Rebind in-app shortcuts in
**Settings → Keyboard**.

## Find a destination or action {#what-s-in-it}

| I want to… | Search or choose |
| --- | --- |
| Open a panel | Its name, such as `Knowledge`, `Memory`, or `Activity`; choose **Open…** to browse |
| Change a setting | `Model`, `Keyboard`, `Theme`, or another Settings section |
| Switch chats | The chat's title; the current chat is marked |
| Run a chat command | `/export`, `/model`, `/clear`, or another listed slash command |
| Use a skill | Its name; the palette places `/skill ` before your draft so you can add instructions and send |
| Ask a quick question | **Ask ‹agent›**, which opens a chat inside the palette |
| Open a plugin | Its view or command name; the row shows the plugin's name |
| Find keyboard actions | `shortcuts`; rows show their current bindings |

Selecting `/incognito` or `/bypass` shows its current state and puts the command
in the composer. Choose the setting and send it yourself. A disabled action
shows why it cannot run; chat-specific actions may need an open conversation.

## Search stored knowledge

Type at least two characters to search the knowledge store as well as commands.
Matches carry a **Knowledge** chip and their source. The last word can be
partial: `postg` can find *Postgres tuning*.

Choose a knowledge result to open **Knowledge → Store** with the same search
and its pending-review filter cleared. If the shortlist is too small, choose
**All matches in Knowledge** to see more. When you see **Knowledge search
unavailable**, check the connection or [troubleshooting guide](/guides/troubleshooting).
Knowledge results require an enabled store; if an expected source is missing,
[check retrieval](/guides/knowledge#check-what-is-being-recalled).

## Talk to fleet members

Choose **Fleet Room** to see members, their status, and recent activity. Click
a member's name to message it directly, or use its control to open its full
console. Start/stop controls appear for eligible local members.

In the room's send bar, start with `@name` to address a member. **Enter sends to
all online members when you name no one.** **⌘↵** (macOS) or **Ctrl+Enter** (Windows/Linux) broadcasts regardless of the
addressed names. This differs from [ordinary chat rooms](/guides/rooms), where
an unaddressed message goes to the lead agent.

A spawned member opened directly on its own port directs you to the host for
Fleet Room. See [Fleet](/guides/fleet) to connect or manage members.

## Browse without typing {#two-lists-not-one}

The empty palette shows recent actions and a short starter list. Type to search
the full catalog, including panels absent from that starter list. Frequently
used actions rise when matches tie; settings or plugin commands can be hidden
by feature flags or unavailable on the current agent.

## Use the desktop quick launcher

Press **⌥Space** on macOS or **Ctrl+Alt+Space** on Windows/Linux to open a
separate palette while another app is focused. Choosing a destination opens
the main window. Escape or losing focus dismisses the launcher. Its global
shortcut has a separate control in **Settings → Keyboard**.

## Add plugin actions {#for-plugin-authors}

See the [palette extension reference](/reference/command-palette) for manifest
commands, inline views, matching, and the frontend registration seam.
