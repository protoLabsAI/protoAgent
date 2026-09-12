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

Nothing to install by hand:

- **The `agent-browser` CLI downloads itself.** With none on PATH, the first browser command
  fetches the **pinned** upstream release for this platform (v0.27.1), from the
  vercel-labs/agent-browser GitHub release. The install is refused unless the download's
  SHA-256 matches the value pinned in `cli_fetch.py`, then it is moved into place
  atomically. It lands in the machine-wide cache, `<box root>/cache/agent-browser/<version>/`.
  Upstream publishes no checksum file, so the pins were computed from the release assets and
  cross-checked against GitHub's own asset digests. This is a runtime download, not a
  bundled binary; the operator ruled on that on 2026-09-12. `cli_autofetch: false` turns the
  automatic download off.
- **Chrome is installed only on request.** The setup banner's **Install Chrome** button runs
  the CLI's own `agent-browser install` (Chrome for Testing, ~150 MB). A tool call never
  does.

If either is missing the plugin reports a **setup gap**: an operator banner in the console
(`GET /api/runtime/status` → `setup_gaps[]`), with a button that runs the fix. The CLI gap
offers **Download agent-browser**, plus **Set the CLI path**. The Chrome gap offers
**Install Chrome**. Each gap is raised at load and re-checked on every failing or recovering
browser command, so it clears with no restart. A CLI on PATH (`npm i -g agent-browser`,
Homebrew, Cargo), or a path pinned in the `binary` setting, always wins over the download.

## Layout

| File | What |
|---|---|
| `tools.py` | the 17 browser tools — subprocess wrappers over the CLI, the byte cap, and the fenced captures |
| `storage.py` | the capture write fence — `browser_screenshot` / `browser_pdf` resolve inside this plugin's instance store, an escape is refused; also the collision-free default filename and oldest-first retention (200 files / 512 MB) |
| `preflight.py` | the setup-gap probe — which CLI resolves (PATH > pinned path > the download), does it have a Chrome to drive — and the banners' buttons |
| `cli_fetch.py` | the pinned, SHA-256-verified CLI download: per-platform pins, atomic install, the box-tier cache |
| `chrome_install.py` | `agent-browser install`, run only from the Install Chrome button |
| `setup_steps.py` | the setup steps behind the Download agent-browser / Install Chrome buttons |
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
