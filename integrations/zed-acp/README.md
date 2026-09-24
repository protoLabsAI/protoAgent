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
| a permission prompt | a parked `approval` (e.g. `run_command`, permanent delete) |
| Stop button | A2A `CancelTask` |
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

## What it does not do (yet)

- **Edits are not routed through Zed.** protoAgent writes its own project roots; Zed shows
  the edit tool card and follows the file, but its *Review Changes* / per-hunk accept does
  not apply. See ADR 0111 D4 for why and for the path to change it.
- A parked **question or form** is shown as text; your next message answers it.
- Images/audio in the prompt are not forwarded; `session/load` (reopening an old thread)
  is not implemented; MCP servers Zed offers in `session/new` are ignored (the instance's
  tools are its own config).
- Tool-call arguments arrive as an 800-char preview, so a huge `write_file` shows its path
  but not its content.

## Development

```bash
cd integrations/zed-acp
uv venv && uv pip install -e . pytest pytest-asyncio
.venv/bin/python -m pytest -q          # unit + fake-A2A + stdio subprocess tests

# drive a live instance exactly as Zed would, printing the session/update stream
# (permission requests are DENIED unless you pass --approve, which clicks "Allow once"):
.venv/bin/python scripts/acp_harness.py --cwd ~/dev/protoAgent \
  --prompt "Read README.md's first 20 lines" \
  -- --url http://127.0.0.1:7870 --trace-frames /tmp/frames.jsonl
```

`examples/protoengineer-transcript.txt` is a real run against a live fleet member.
