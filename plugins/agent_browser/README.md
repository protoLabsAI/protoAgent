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
browser_snapshot()           # interactive elements with @e1, @e2… refs (full=true: whole tree)
browser_click("@e2")         # act on a ref (or a CSS selector)
browser_wait(text="Saved")   # wait for the result: selector / text / url / load / js
browser_fill("@e3", "…")
browser_get_text("body")     # read / extract
browser_pdf("page.pdf")      # or browser_screenshot("page.png")
browser_close()
```

Two guards sit on that loop. **Walls:** `browser_open` checks the landed page, and a
Cloudflare / CAPTCHA / DataDome / PerimeterX / Akamai / Google-sorry interstitial, an HTTP
403/429, `ERR_BLOCKED_BY_CLIENT` or a sign-in wall comes back as `Error: blocked by <wall> at
<url> — … ask the user to clear it in the console Browser panel, then continue` instead of a
`✓` (a self-clearing JS challenge is re-checked for ~5 s first; `detect_walls: false` turns it
off). **Boundaries:** page-derived output (snapshot, get_text, get_html, form_read; `browser_eval` stays raw)
arrives inside the CLI's `AGENT_BROWSER_PAGE_CONTENT nonce=… origin=…` markers
(`content_boundaries`, default on), so the model can tell page data from instructions. A
consumer that parses a result takes the text between the markers (`boundaries.strip()`).

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
| `tools.py` | the 21 browser tools — subprocess wrappers over the CLI, the byte cap (reading tools return a marked first part), `browser_wait`, the fenced captures, and the action gate's hooks |
| `gate.py` | the action gate — resolves what a click / Enter / eval / upload will really hit (`@eN` refs included), decides by `confirm_actions` (default `submit`), asks the operator with an approval card, refuses where no one can answer; plus the `agent_browser.add_gate_policy` / `describe_target` services for other plugins |
| `storage.py` | the capture write fence — `browser_screenshot` / `browser_pdf` resolve inside this plugin's instance store, an escape is refused; also the collision-free default filename and oldest-first retention (200 files / 512 MB) |
| `preflight.py` | the setup-gap probe — which CLI resolves (PATH > pinned path > the download), does it have a Chrome to drive — and the banners' buttons |
| `cli_fetch.py` | the pinned, SHA-256-verified CLI download: per-platform pins, atomic install, the box-tier cache |
| `chrome_install.py` | `agent-browser install`, run only from the Install Chrome button |
| `setup_steps.py` | the setup steps behind the Download agent-browser / Install Chrome buttons |
| `browser_panel.py` | the Browser panel page + routes — the interactive canvas, the gated nav / stream-ticket / WS-stream routes |
| `browser_stream.py` | the CDP bridge — screencast frames out, input in, viewport resize + nav re-arm; the WS ticket auth |
| `walls.py` | bot-wall / sign-in-wall detection after `browser_open` — a dependency-free probe (`PROBE_JS`) + classifier (`check`) other plugins can reuse |
| `runner.py` | the one hardened subprocess path (aggregate byte cap, process-group kill, bounded joins, stdin feeder) the tools and the client both run on, plus the process-wide per-session guard |
| `client.py` | the browser client OTHER plugins get from the `agent_browser.client` service — see [For plugin authors](#for-plugin-authors-the-browser-client) |
| `boundaries.py` | the CLI's untrusted-content markers: per-call flag, parse/re-wrap for `browser_form_read`, `strip()` for consumers that parse a result |
| `runtime.py` | what the tools and the panel share: the launch-flag builder (headed / profile / device / stealth), the session name + child env (`cli_env`), the idle reaper, the profile-in-use error |
| `skills/web-browse/` | the discovery skill (defers to `agent-browser skills get core`) |
| `workflows/` | declarative browser recipes (browse-and-extract, fill-form — fills and verifies, never submits), run on the workflow-only `browser` subagent |
| `__init__.py` | `register()` — preflight, tools, the interactive panel; skills/workflows auto-discovered |

Tests live with the host, in [`tests/test_agent_browser_plugin.py`](../../tests/test_agent_browser_plugin.py)
and [`tests/test_agent_browser_gate.py`](../../tests/test_agent_browser_gate.py) (host-free: the CLI
is mocked), and [`tests/test_agent_browser_forms_live.py`](../../tests/test_agent_browser_forms_live.py)
/ [`tests/test_agent_browser_gate_live.py`](../../tests/test_agent_browser_gate_live.py) (real browser —
see below); walls and boundaries in [`tests/test_agent_browser_walls.py`](../../tests/test_agent_browser_walls.py)
(fixtures per wall class and negatives) and [`tests/test_agent_browser_walls_live.py`](../../tests/test_agent_browser_walls_live.py)
(real browser against local interstitial / login-redirect / 403 / 429 pages). The operator knobs (headed, allowed domains, profile, device,
stealth, …) are editable in **Settings ▸ Plugins ▸ Agent Browser**, or under `agent_browser:`
in `langgraph-config.yaml`.

## Which browser: one per agent (and optionally one per chat)

The CLI keeps a browser per session name and calls it `default` when none is given, so every
protoAgent instance on a machine used to drive ONE Chrome with ONE cookie jar. The plugin now
names the session itself — `runtime.instance_session_name()`, `pa-<instance-id>-<sha of the
instance root>` — and passes it, with `runtime.cli_env`, to **every** CLI subprocess: the
tools, the panel's `/nav` route and the screencast's `get cdp-url`. An explicit env copy per
child, not `os.environ`: it is process-global, and per_session resolves a different name per
concurrent chat, so a process-wide value would race. `session_scope: per_session` names each
chat's browser `<instance name>-<sha of the chat id>`. An operator `AGENT_BROWSER_SESSION`
still names the main browser outright.

Idle reaping is the CLI's `AGENT_BROWSER_IDLE_TIMEOUT_MS` (read once when a daemon is
spawned; every command resets it): `idle_timeout_min` (30) for per-chat browsers,
`main_idle_timeout_min` (0 = never) for the main one, because the panel's screencast talks
CDP directly and never resets the daemon's timer. A `profile` DIR can back one Chrome at a
time; a second launch on it fails with Chrome's `ProcessSingleton` error, which the tools
and the panel turn into a readable `Error:` naming the cause and the fix.

**Upgrading:** an agent's page in the old shared `default` session isn't carried over — the
first browser call after the upgrade starts the agent's own (empty) browser. The `default`
daemon keeps running until it's closed (`agent-browser --session default close`) or the
machine reboots.

## For plugin authors: the browser client

Another plugin that needs its own browser gets it through the `agent_browser.client` service
(ADR 0116), never an import and never its own `subprocess.run`:

```python
make = getattr(sdk, "service", lambda _n: None)("agent_browser.client")
browser = make(session="shop", profile="~/.protoagent/shop-profile", headed=True, own_tab="shoptab")
r = browser.open(url)          # r.kind: ok | timeout | wall | cli_error | unavailable | bad_argument | …
```

`client.BrowserClient` runs every command through `runner.run` (the tools' byte cap,
process-group kill and CLI resolution) on the plugin's own named session (instance-prefixed by
default), takes the same per-session guard as the tools, sends the launch flags on every
`open <url>` and never a URL-less `open`, gives every wait an explicit timeout below the
CLI's 30 s IPC re-send, cuts a hung navigation off and recovers once, can own a labelled tab
(the Gemini-panel guard proven in ebay-plugin), and runs the `walls.py` check after a
navigation. Sync and thread-safe; every method returns a typed `Result`. The full contract:
[docs/reference/browser-automation.md › For plugin authors](../../docs/reference/browser-automation.md#for-plugin-authors-the-browser-client).
Tests: [`tests/test_agent_browser_client.py`](../../tests/test_agent_browser_client.py) (scripted
CLI) and [`tests/test_agent_browser_client_live.py`](../../tests/test_agent_browser_client_live.py)
(real browser).

## Live form tests (real browser)

The form tools (`browser_select`, `browser_upload`, `browser_form_read`, `browser_click`'s
JS fallback) run their logic as **in-page JavaScript** (`forms.py`). The mocked unit tests
never execute that JS, so a vendor markup change — or a regression in the in-page code —
would ship green. [`tests/test_agent_browser_forms_live.py`](../../tests/test_agent_browser_forms_live.py)
closes that gap: it drives the **real** tools from `get_browser_tools` against saved,
fully self-contained application-form fixtures in
[`tests/fixtures/ats/`](../../tests/fixtures/ats/), opened over `file://` with **no
network**, and asserts the widgets behave (the visa combobox commits "Yes", not the
highlighted "Yes, Ireland…"; a phone country then number both read back; an upload reads its
filename back; a wrong option is a hard `Error:`; a label still resolves after a reload).

```bash
pytest tests/test_agent_browser_forms_live.py tests/test_agent_browser_form_labels_live.py
```

`test_agent_browser_form_labels_live.py` covers what the Career Coach auto-apply eval
found on real Lever, Workable and Ashby postings. Those fixtures (`lever.html`,
`workable.html`, `ashby_questions.html`, `places_autocomplete.html`) are modeled from the
live forms, so a custom question reads by its question text and not by
`cards[<uuid>][field0]`. They also cover checkbox groups as one field, `browser_select` on
group questions and autocomplete boxes, `|` alternatives, combobox options in the read, and
the `browser_fill` read-back.

Live tests use their own session names and close them in teardown; each also sets
`main_idle_timeout_min`, so a run that's killed before teardown leaves a daemon that exits on
its own a few minutes later instead of living until reboot.

The whole module **skips** unless the `agent-browser` CLI resolves AND Chrome is available
(the plugin's own `preflight` probe decides), so the default gate stays host-free and green.
To run them you need the CLI (`npm i -g agent-browser`, or let the plugin auto-fetch it) and
Chrome (`agent-browser install`).

**Refreshing a fixture when a vendor changes markup.** Each file under `tests/fixtures/ats/`
opens with an HTML comment recording the live form it models and the date it was captured.
When the Greenhouse or Ashby form drifts, open the live form, read the relevant widget's DOM
in devtools, and update the matching block in the fixture — keep the class names `forms.py`
keys on (`select__control`, `select__single-value`, `select__menu`, `iti__selected-flag`,
`iti__country`, `iti__country-name`), and keep the `<label for>` associations and the
`<fieldset>`/`<legend>` around radio groups — then bump the "last modeled" date and re-run
the live tests.

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
