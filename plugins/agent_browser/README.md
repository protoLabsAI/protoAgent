# `agent_browser` — browser automation (bundled)

Gives the agent a real browser, backed by
**[agent-browser](https://github.com/vercel-labs/agent-browser)** (vercel-labs) — a fast
native-Rust CLI/daemon that drives Chrome over CDP with **accessibility-tree snapshots**
and compact `@eN` element refs.

**Bundled in-tree** (#3451), superseding the standalone `agent-browser-plugin` repo — see
the [Browser automation guide](../../docs/guides/browser-automation.md) for setup, the
panel, and the fence. **Ships disabled:**

```yaml
plugins:
  enabled: [agent_browser]
```

The model's loop is **open → snapshot → act on a `@ref` → verify**:

```
browser_open("example.com")
browser_snapshot()           # accessibility tree with @e1, @e2… refs
browser_click("@e2")         # act on a ref (or a CSS selector)
browser_fill("@e3", "…")
browser_get_text("body")     # read / extract
browser_pdf("page.pdf")      # or browser_screenshot("page.png")
browser_close()
```

## Requirements

The **`agent-browser`** binary on PATH — nothing in protoAgent installs it:

```bash
npm i -g agent-browser && agent-browser install   # the second step downloads Chrome for Testing
```

(Homebrew, Cargo, and the upstream standalone binaries work too.) If the CLI or its
Chrome is missing, the plugin reports a **setup gap** — an operator banner in the
console's status (`GET /api/runtime/status` → `warnings[]`), raised at load and re-checked
on every failing or recovering browser command, so the next call after you fix the setup
clears it with no restart. On the desktop app the Tauri shell inherits your **login shell's**
PATH, which is how an nvm-installed CLI is found; a pinned absolute path in the `binary`
setting is the robust alternative.

A managed, pinned + checksummed download of the binary is deliberately **not** here (see
`__init__.py`) — it needs an ADR and a ruling on the no-bundled-`br` order first.

## Layout

| File | What |
|---|---|
| `tools.py` | the 17 browser tools — subprocess wrappers over the CLI, the byte cap, and the fenced captures |
| `storage.py` | the capture write fence — `browser_screenshot` / `browser_pdf` resolve inside this plugin's instance store, an escape is refused; also the collision-free default filename and oldest-first retention (200 files / 512 MB) |
| `preflight.py` | the setup-gap probe — is the CLI resolvable, and does it have a Chrome to drive |
| `browser_panel.py` | the Browser panel page + routes — the interactive canvas, the gated nav / stream-ticket / WS-stream routes |
| `browser_stream.py` | the CDP bridge — screencast frames out, input in, viewport resize + nav re-arm; the WS ticket auth |
| `runtime.py` | shared launch-flag builder (headed / profile / device / stealth), used by the tools and the panel |
| `skills/web-browse/` | the discovery skill (defers to `agent-browser skills get core`) |
| `workflows/` | declarative browser recipes (browse-and-extract, fill-form) |
| `__init__.py` | `register()` — preflight, tools, the interactive panel; skills/workflows auto-discovered |

Tests live with the host, in [`tests/test_agent_browser_plugin.py`](../../tests/test_agent_browser_plugin.py).
The operator knobs (headed, allowed domains, profile, device, stealth, …) are editable in
**Settings ▸ Plugins ▸ Agent Browser**, or under `agent_browser:` in
`langgraph-config.yaml`.

## The Browser panel

A console view (ADR 0026) that is a **fully drivable viewport**: a live CDP screencast
(event-driven JPEG frames, not a screenshot poll) painted on a `<canvas>`, with your
mouse / keyboard / scroll forwarded back into the page via `Input.dispatch*`. Everything
rides a **gated same-origin WebSocket**, so it works on the host and a remote fleet member
alike. The viewport resizes Chrome's layout viewport to your dock (× device-pixel-ratio),
re-arms the screencast on every navigation, and keeps updating when the panel isn't
focused.

**Where the auth is:** the page route is public *chrome* (the host auto-exempts every
`views[].path` — an iframe navigation can't carry a bearer). The capability is gated:
`POST /nav` and `POST /stream-ticket` ride the operator bearer under
`/api/plugins/agent_browser`, and `WS /stream` self-gates with a single-use ticket minted
only from that gated route, because the host's auth middleware is HTTP-only.
