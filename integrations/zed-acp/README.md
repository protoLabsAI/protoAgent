# protoagent-acp — protoAgent in Zed's Agent Panel

A small stdio [Agent Client Protocol](https://agentclientprotocol.com) server that lets
Zed (or any ACP client: JetBrains, Neovim, Emacs…) talk to a **running** protoAgent
instance. It is a pure A2A client of that instance: it works the same against the
desktop hub, a fleet member, or a remote box, and nothing of protoAgent needs to be
installed where the editor runs. Design and trade-offs: [ADR 0111](../../docs/adr/0111-zed-operator-editor-acp-shim.md).

```
Zed ──ACP/stdio──▶ protoagent-acp ──A2A 1.0 (HTTP+SSE)──▶ protoAgent /a2a
```

| You see in Zed | Comes from |
|---|---|
| streamed answer text | A2A artifact updates (terminal replace de-duplicated) |
| "thinking" | reasoning-v1 parts |
| tool cards with kind (read / search / edit / execute / …) | tool-call-v1 metadata |
| **follow-the-agent** jumps into files | `project` + `path` (+ `offset`) args, resolved to absolute paths |
| search-hit locations on a finished `search_files` | the `file:line:` hits in its result |
| a permission prompt: **Allow once / Allow for this session / Deny** | a parked `approval` (e.g. `run_command`, permanent delete) |
| Stop button | A2A `CancelTask`, sent after a short grace window (see Send Now below) |
| **Send Now** on a queued message | **steers the running turn**: the message is queued into it with protoAgent's mid-turn steering, and the turn carries on with it |
| thread history: list and reopen past threads, **console chats included** | `GET /api/chat/sessions` + `…/turns`; a reopened thread continues on the same session, so the agent keeps its memory |
| a new thread that picks up the chat you handed off from the console | `POST /api/editor/handoff/claim` on `session/new` |
| `show_code` / `open_in_editor` cards you can follow (each only when its toolset is on: `filesystem.code_pane` / `filesystem.editor_command`) | the file and line in their args |
| an error callout + a "⚠️ protoAgent error: …" line | a turn that FAILED (e.g. the model's 429 usage limit), or a stream that closed without a terminal state and whose task (read back with `GetTask`) failed or is still running |

Each Zed thread is one protoAgent chat session (`chat-zed-…`), so the conversation also
appears in the protoAgent console.

## Try it in Zed

Add to Zed's `settings.json` (`zed: open settings`), then pick **protoAgent** in the Agent
Panel's new-thread menu:

```jsonc
{
  "agent_servers": {
    // the default local instance
    "protoAgent": {
      "type": "custom",
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/protoLabsAI/protoAgent@spike/zed-acp-shim#subdirectory=integrations/zed-acp",
        "protoagent-acp", "--url", "http://127.0.0.1:7870"
      ],
      // only if the instance has a bearer (Settings → auth.token / A2A_AUTH_TOKEN)
      "env": { "PROTOAGENT_TOKEN": "" }
    },
    // a desktop fleet member (here protoEngineer on :7875) — the hub's fleet token works
    "protoEngineer": {
      "type": "custom",
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/protoLabsAI/protoAgent@spike/zed-acp-shim#subdirectory=integrations/zed-acp",
        "protoagent-acp", "--url", "http://127.0.0.1:7875",
        "--token-file", "~/Library/Application Support/studio.protolabs.protoagent/workspaces/.fleet-token"
      ]
    }
  }
}
```

- From a local checkout instead: `"args": ["--from", "/path/to/protoAgent/integrations/zed-acp", "protoagent-acp", …]`.
- **Credential**: `--token-file FILE`, or `PROTOAGENT_TOKEN` in `env`, or run
  `uvx --from … protoagent-acp login` once (stores URL + token in
  `~/.config/protoagent-acp/credentials.json`, mode 0600 — Zed's "terminal" auth method runs
  exactly this). Desktop fleet members accept the hub's fleet token.
- **A fleet member through the hub**: `--url <hub> --slug <member>`.
- **A remote instance** (its project paths are on another machine): map each project to
  your local checkout with `--root protoAgent=/Users/me/dev/protoAgent` so follow-the-agent
  lands in *your* files. Roots are re-read when the agent names a project it onboarded
  mid-session.
- Logs: `dev: open acp logs` in Zed (the shim logs to stderr; stdout is protocol only).

### "Allow for this session"

A `run_command` approval offers **Allow for this session** as well as Allow once and Deny.
Choosing it approves the command and switches the Zed thread to allow-all. The shim then:

- stamps `bypass_permissions: true` on every later A2A message in that thread. This is the
  same `message.metadata` key the console's `/bypass` sends;
- auto-approves any further command approval in the **current** turn, because the server's
  bypass only takes effect from the next message;
- writes "Commands will run without asking for the rest of this thread." into the
  transcript.

This state is per thread and held in memory only, so a new Zed thread asks again. If the
instance forbids bypass (`filesystem.bypass_allowed: false`), the server keeps asking and so
does the shim; it says so once and never works around the refusal. **Permanent deletes
always ask.** They are never offered "for this session" and never auto-approved, which
matches the server's delete floor.

### Send Now steers the running turn

Zed queues a message you type while the agent is working and sends it when the turn ends.
**Send Now** (press Enter twice on the queued message) is `session/cancel` followed by
`session/prompt`. Zed's own steering is disabled for external agents. The shim turns that
pair back into a steer:

- **On cancel**, the prompt answers `cancelled` at once, but the protoAgent turn keeps running
  unseen for `--steer-grace` seconds (default 1.5).
- **If a prompt arrives inside that window**, its text is queued into the running turn
  (`POST /api/chat/sessions/<id>/steer`, the console's mid-turn steering). The agent folds it
  in at its next model call. The new prompt picks up the same stream, and a
  `↪ steering: …` line marks where the agent read it.
- **If no prompt arrives, it is a real Stop**: `CancelTask` goes out when the window closes.
  The cost is that a plain Stop cancels up to `--steer-grace` seconds late on the server,
  though Zed's UI stops immediately. `--steer-grace 0` restores an instant cancel and turns
  steering off.
- **Fallbacks**:
  - If the steer can't be queued, the shim cancels and starts a normal new turn.
  - If the turn had already finished, the message is a normal new turn.
  - If the steer arrived after the turn's last model call, the shim takes it back out of the
    queue and runs it as the next turn, the same reconciliation the console does.
- **An approval that parks inside the window** is asked of the new prompt. If an approval
  was already on screen, Zed's cancel dismisses it, which answers "deny".

### Thread history, console chats included

Zed's thread history lists **every** chat this agent has, including conversations started
in the protoAgent console, not just the ones started from Zed. Pass `--zed-threads-only`
to list only `chat-zed-…` threads.

- **Opening a thread** replays it: your messages, Send Now steers where the agent read them,
  each turn's tool calls with their file locations, and the answers.
- **Your next message** continues on the same protoAgent session, so the agent remembers the
  conversation, including one you started in the console.
- **Titles** are the server's session title when it has one. Otherwise the shim uses its
  local index (`~/.config/protoagent-acp/threads.json`), then the first message.
- **Folders:** a chat with no recorded folder is listed under the folder Zed asked about.

### Continue a console chat in Zed

Press **Continue in Zed** in the console, or have the agent run `open_in_editor`. Then, within
2 minutes, start a thread for the agent in Zed's Agent Panel. `session/new` claims the
hand-off (`POST /api/editor/handoff/claim {cwd}`), and the new thread **is** that console
chat:

- it keeps the same session, with the history replayed;
- it jumps to the file the console was showing;
- it adds the line: ↪ Continuing your console chat "<title>".

With no hand-off waiting (204), an expired one, or a server without the route, you get a
fresh thread as usual.

**A chat waiting on a question or form.** If the chat you open or continue is parked on
a question or a form (for example `request_user_input`), the replay shows it, with a
form's fields as a list, followed by: "This chat is waiting on a form from the console:
<question> Your next message here will be sent as the answer — or answer it in the
console."

- **Only after that notice** is your next message sent as the answer, and only once.
- **If you answered it in the console meanwhile,** your message starts a normal new turn.
- **A pending approval** is never answered from Zed: a stray message must not read as
  "approved". Approve or deny it in the console.
- **A turn still running in the console** is shown as "(still running in the console…)"
  rather than as a half-written answer. The shim fills in its finished answer before your
  message goes out.

**It never talks over the console.** Before each message, the shim checks whether a turn is
already running on that chat (`GET /api/chat/sessions/<id>` → `active`). If one is, it says
"This chat is busy in the console. I'll send when it's free.", checks every 2 seconds, and
gives up with an error after 120 seconds without sending anything. A server that doesn't
report `active` isn't waited on.

## What it does not do (yet)

- **Edits are not routed through Zed.** protoAgent writes its own project roots; Zed shows
  the edit tool card and follows the file, but its *Review Changes* / per-hunk accept does
  not apply. See ADR 0111 D4 for why and for the path to change it.
- A parked **question or form** is shown as text; your next message answers it.
- Images/audio in the prompt are not forwarded; MCP servers Zed offers in `session/new` are ignored (the instance's
  tools are its own config).
- Tool-call arguments arrive as an 800-char preview, so a huge `write_file` shows its path
  but not its content.

## Development

```bash
cd integrations/zed-acp
uv venv && uv pip install -e . pytest pytest-asyncio
.venv/bin/python -m pytest -q          # unit + fake-A2A + stdio subprocess tests

# drive a live instance exactly as Zed would, printing the session/update stream
# (permission requests are DENIED unless you pass --approve ("Allow once") or
#  --approve-always ("Allow for this session", falling back to "Allow once")):
.venv/bin/python scripts/acp_harness.py --cwd ~/dev/protoAgent \
  --prompt "Read README.md's first 20 lines" \
  -- --url http://127.0.0.1:7870 --trace-frames /tmp/frames.jsonl
```

`examples/protoengineer-transcript.txt` and `examples/navaengineer-steer-and-history.txt`
are real runs against live fleet members. The harness also drives the newer flows:
`--send-now TEXT --send-now-after SECONDS` (Zed's Send Now), and `--list` / `--load ID`
(thread history).
