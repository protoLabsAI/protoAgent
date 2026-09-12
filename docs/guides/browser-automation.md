# Browser automation

Give the agent a real browser — open pages, read them as an accessibility tree, click
and type, take screenshots, print to PDF — and drive the same browser yourself from a
console panel while the agent works in it.

That's the **`agent_browser`** plugin, bundled in-tree under
[`plugins/agent_browser/`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/agent_browser).
It is a thin shell over
**[agent-browser](https://github.com/vercel-labs/agent-browser)** (vercel-labs), a native
Rust CLI/daemon that drives Chrome over CDP. protoAgent does not reimplement browser
automation or a renderer — it wraps that CLI as tools, a skill, workflows and a view.

**It ships disabled, and it needs one external binary.** Both on purpose: 17 tools and
unrestricted network reach are a deliberate choice, and nothing in protoAgent installs a
third-party native binary for you.

## Install it

```bash
npm i -g agent-browser     # the CLI (Homebrew, Cargo, and the upstream release binaries work too)
agent-browser install      # downloads Chrome for Testing — the browser the CLI drives
agent-browser doctor       # sanity check: environment, Chrome, a live headless launch
```

Then turn the plugin on:

```yaml
plugins:
  enabled: [agent_browser]
```

If the CLI or its Chrome is missing, the plugin says so where you'll see it: a **setup-gap
banner** in the console's operator status (`GET /api/runtime/status` → `warnings[]`), with
a separate line for each — "the CLI isn't on PATH" carries a link into the plugin's own
settings so you can pin a full path, and "no Chrome to drive" tells you to run
`agent-browser install`. Both are re-checked whenever a browser command runs, so once you
fix the setup the next call clears the banner — no restart.

::: tip The desktop app and PATH
The desktop shell finds a CLI installed by **nvm** only because it inherits your login
shell's PATH. If a terminal can run `agent-browser` but the desktop app reports the gap,
set the plugin's **`agent-browser` binary** setting to the absolute path
(`which agent-browser`).
:::

A managed, pinned-and-checksummed download of the binary is deliberately **not** part of
this plugin yet — fetching a third-party native binary at runtime is an architectural
decision (and a policy one), so it's a separate slice behind an ADR. Until then the
plugin is PATH-only plus that setup gap.

## How the agent uses it

The loop is **open → snapshot → act on a ref → verify**. `browser_snapshot` returns the
page's accessibility tree with compact `@eN` refs, and the action tools take a ref or a
CSS selector:

```
browser_open("example.com")
browser_snapshot()               # … button "Sign in" @e7 …
browser_click("@e7")
browser_fill("@e9", "hello")
browser_get_text("body")         # read / extract
browser_close()
```

The bundled **`web-browse` skill** teaches that loop and then defers to the CLI's own,
always-version-matched guidance (`agent-browser skills get core`), so the instructions
can't go stale against a newer binary. Two **workflows** ship as recipes —
`browse-and-extract` and `fill-form`.

Every tool degrades to a readable `Error: …` string instead of raising: a failed browser
action should inform the agent's loop, not crash the turn. Output is capped
(`max_response_bytes`, 200 KB by default) because page text is untrusted and unbounded —
over the cap the child is killed and the tool returns a bounded diagnostic rather than
flooding the model's context.

### Screenshots and PDFs are fenced

`browser_screenshot` (PNG) and `browser_pdf` (Chrome's print-to-PDF) both return the
**absolute path** of the file they wrote. Pass a filename or a relative path — files land
inside the plugin's own per-instance capture directory, and an absolute path outside it is
**refused, not redirected**. That's what the manifest's `filesystem: scoped` capability
means here: a page that says "save a screenshot to `~/.ssh/authorized_keys`" cannot pick
the target.

Three details worth knowing:

- **Leave the path blank** and the file is named for you (`page-20260911-174233-9f3a.pdf`).
  Two unnamed captures then never overwrite each other — which they did when both defaulted
  to `page.pdf`.
- **"Saved to …" means this call's bytes are on disk, and the old file is never at risk.**
  Every capture is written to a short temporary name beside the target and swapped into
  place with one atomic rename, only once it's non-empty. So a run that writes nothing (or
  zero bytes) is an error and leaves any previous file of that name untouched; a cancelled
  or killed capture can leave at most a disposable temp file (swept once it's an hour old);
  and when two captures race for one name, the last to finish wins without deleting the
  other's output. Printing a blank page (`about:blank` with nothing on it) is refused up
  front — if you have HTML rather than a URL, open it as a `data:text/html,…` or `file://`
  URL, or write it into the blank page with `browser_eval` first.
- **PDFs are always US Letter.** `agent-browser pdf` has no paper-size option and ignores a
  page's CSS `@page size`: an A4 page prints at 612 × 792 pt (Letter), not 595 × 842.
  Design pages that will be printed for Letter, and don't promise A4. (A live test pins this,
  so if upstream starts honouring `@page` the suite says so.)
- **Captures are disposable.** The directory is pruned oldest-first past 200 files or
  512 MB. Anything you want to keep should go to `save_file_artifact` (which copies the
  bytes into its own store) or a project folder. A capture over the artifact plugin's
  25 MB `max_blob_kb` default is flagged in the tool's reply, because `save_file_artifact`
  would otherwise refuse it with no hint as to which capture was too big.

`browser_pdf` is the HTML→PDF route. Open a page — or an HTML file you generated yourself,
via a `file://` URL — print it, then hand the returned path to `save_file_artifact` so the
user gets a download card in the [Artifact panel](/adr/0038-generative-ui-artifacts-two-mode).
That's how an agent delivers a real PDF resume, report or invoice.

`protoagent config explain` prints the instance root if you want to find the files on
disk.

## The Browser panel

The plugin ships a console view ([ADR 0026](/adr/0026-plugin-contributed-console-surfaces)) that is a
**fully drivable viewport**, not a screenshot poll: a live CDP **screencast** (event-driven
JPEG frames) painted onto a `<canvas>`, with your mouse, keyboard and scroll forwarded
back into the page as `Input.dispatch*`. You and the agent are in the same browser — take
over a login wall by hand, then let it continue.

It resizes Chrome's layout viewport to your dock (× device-pixel-ratio) so pages reflow to
fill rather than sitting letterboxed, re-arms the screencast on every navigation so you see
the agent move through pages, and keeps updating when the panel isn't focused. Sharpness is
`stream_quality` (JPEG, 1–100).

Everything rides a **same-origin WebSocket**, which is what makes the panel work on a
remote fleet member through the hub proxy ([ADR 0042](/adr/0042-fleet-supervisor-unified-console)),
not just on the host.

**Where the auth is.** The page route is public *chrome* — the host auto-exempts every
manifest `views[].path` from the operator bearer gate, because the console iframes it with
a plain navigation that cannot carry a header. The *capability* is what's gated: `POST
/nav` and `POST /stream-ticket` live under `/api/plugins/agent_browser` and ride the
operator bearer, and the `WS /stream` bridge self-gates with a **single-use, 30-second
ticket** minted only by that gated route — because the host's auth middleware is HTTP-only
and does not cover WebSocket handshakes.

## Settings

Editable in **Settings ▸ Plugins ▸ Agent Browser**, or under `agent_browser:` in
`langgraph-config.yaml`. Blank / `0` / `false` means "the CLI's own default".

| Setting | What it does |
|---|---|
| `binary` | The `agent-browser` CLI. Override with an absolute path when PATH isn't enough. |
| `timeout_s` · `max_response_bytes` | Per-command subprocess timeout; the aggregate stdout+stderr byte cap. |
| `home_url` | The page the panel opens to. Set it and the panel auto-opens it when nothing is open; blank gives a Start button. |
| `stream_quality` | Panel JPEG quality (1–100). |
| `headed` | Show a real browser window instead of running headless. |
| `profile` | A browser profile directory — isolation plus persisted auth/cookies across sessions. |
| `device` | Device emulation, e.g. `iPhone 16 Pro`. |
| `allowed_domains` | A navigable-domain allowlist. The tightest lever you have on where the agent can go. |
| `confirm_actions` | Action categories that require confirmation first. |
| `max_output` | Cap the page text returned to the model (the CLI's own LLM-safety knob). |
| `stealth` · `user_agent` · `browser_args` | Anti-detection and raw Chrome launch args — see below. |

Launch options apply when the **session launches** (the first `open`). A session already
running keeps its setup until it is closed and reopened. A session started from the panel's
Start button gets the same flags as one the agent opens.

### Pages that block automation

Google, Reddit and Cloudflare-fronted sites detect and refuse automated browsers. The
levers, weakest to strongest:

- `stealth: true` — drops the `navigator.webdriver` automation flag and, when headless,
  swaps the give-away `HeadlessChrome` User-Agent for a real desktop one;
- `user_agent` / `browser_args` — the same thing by hand, for fine control;
- `headed: true` **plus** a logged-in Chrome `profile` — much the most reliable, because
  it is a real window with real session state.

No setting defeats detection entirely, and none of them change the fact that you are
responsible for what the agent does on a site.

## Where things live

| File | What |
|---|---|
| `tools.py` | the 17 tools — subprocess wrappers, the byte cap, the fenced captures |
| `storage.py` | the capture write fence |
| `preflight.py` | the setup-gap probe (CLI on PATH, Chrome resolvable) |
| `browser_panel.py` | the panel page + its gated nav / ticket / stream routes |
| `browser_stream.py` | the CDP bridge — frames out, input in, resize + nav re-arm, the WS ticket |
| `runtime.py` | the launch-flag builder shared by the tools and the panel |
| `skills/` · `workflows/` | the `web-browse` skill and the two recipes |

See also: [Plugins](/guides/plugins) · [Building a plugin view](/guides/building-react-plugin-views)
· [Sandboxing & egress](/guides/sandboxing).
