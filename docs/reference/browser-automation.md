# Browser automation settings and internals

<span id="browser-automation-settings-and-internals"></span>

For setup and everyday use, follow [Browser automation](/guides/browser-automation). This page owns provisioning, capture limits, launch settings, and the panel’s implementation.

## The CLI and its Chrome

The plugin needs the `agent-browser` CLI and a Chrome for it to drive. You don't have to
install either by hand.

- **The CLI downloads itself.** With no `agent-browser` on PATH, the first browser command
  (or the panel's Start button) downloads the pinned upstream release for your platform. It
  is **v0.27.1**, the version the plugin is tested against, fetched from the
  vercel-labs/agent-browser GitHub release. The download is **refused unless its SHA-256
  matches the value pinned in the plugin**, then moved into place atomically, so a
  corrupted or tampered download never becomes the binary that runs. It lands in the
  machine-wide cache (`<box root>/cache/agent-browser/0.27.1/`, see
  `protoagent config explain`), so every instance on the machine shares one copy. Builds
  are pinned for macOS (arm64, x64), Linux (x64, arm64, glibc and musl) and Windows x64.
  On any other platform the banner tells you to install it yourself. Turn
  **Download the CLI on first use** (`cli_autofetch`) off to never download automatically.
- **Chrome installs only when you ask.** It's a ~150 MB download into
  `~/.agent-browser/browsers`, so no tool call ever starts it. The setup banner's
  **Install Chrome** button runs the CLI's own `agent-browser install` for you. Chrome for
  Testing has no Linux ARM64 build, so on that platform install Chromium from your package
  manager instead.

Anything still missing shows up as a **setup-gap banner** in the console, one per concern,
each with a button that fixes it:

- **"the CLI isn't on PATH"** has **Download agent-browser**, the same pinned download,
  started now. While it runs the banner says so, and it clears once the CLI is verified.
  A failed download says why and offers **Retry**. **Set the CLI path** opens the plugin's
  settings.
- **"no Chrome to drive"** has **Install Chrome**. The banner shows progress while the
  install runs, clears when it finishes, and shows the CLI's own error with a Retry if it
  fails.

Both are also re-checked whenever a browser command runs, so a setup you fix by hand clears
its banner on the next call, with no restart.

**Your own install always wins.** A CLI on PATH (`npm i -g agent-browser`, Homebrew, Cargo)
is used ahead of the download, and so is a path you set in the plugin's **`agent-browser`
binary** setting. A download stands in only for the stock `agent-browser` command name.

::: tip The desktop app and PATH
The desktop shell sees a CLI installed by **nvm** only because it inherits your login
shell's PATH. If you'd rather use that install than the download, set the plugin's
**`agent-browser` binary** setting to its absolute path (`which agent-browser`).
:::

```bash
agent-browser doctor       # sanity check, whichever CLI you use: environment, Chrome, a live launch
```

## How the agent uses it

The loop is **open → snapshot → act on a ref → verify**. `browser_snapshot` returns the
page's interactive elements (fields, buttons, links, headings) with compact `@eN` refs, and
the action tools take a ref or a CSS selector:

```
browser_open("example.com")
browser_snapshot()               # … button "Sign in" @e7 …
browser_click("@e7")
browser_wait(text="Welcome")     # wait for what the click should bring — not a sleep
browser_fill("@e9", "hello")
browser_get_text("body")         # read / extract
browser_close()
```

### Perception: snapshot to act, get_text to read

`browser_snapshot()` runs the CLI's interactive, compact snapshot (`snapshot -i -c`). It
keeps every element the action tools can target, and headings for orientation, but drops
body text — read content with `browser_get_text` instead. Measured on live pages:

| Page | Default (`-i -c`) | `full=true` |
|---|---|---|
| Wikipedia, *Python (programming language)* | 71 KB (~18K tokens) | 381 KB (over the 200 KB cap) |
| Greenhouse job application | 2.8 KB (~0.7K tokens) | 23.8 KB |
| Hacker News front page | 13 KB | 27 KB |

Options: `full=true` for the whole tree with text, `scope="<css>"` to snapshot one region
(`-s`), and `depth=N` to limit tree depth (`-d`). Link URLs (`--urls`) are not included
because they more than double a link-heavy page (71 KB → 184 KB on the Wikipedia page).

`browser_wait` waits for **one** of these conditions: `selector` (CSS, visible), `text`
(appears on the page), `url` (a substring of the URL; CLI 0.27.1 matches a substring, not
a glob), `load` (`load` / `domcontentloaded` / `networkidle`), or `js` (a truthy
expression). The timeout is `timeout_s`, at most 25 s, and always below the plugin's
`timeout_s`. Each wait passes its timeout to the CLI explicitly. The daemon's default (30 s)
is the same as the CLI's IPC read timeout, so a wait that never matched made the CLI resend
it five times, and the session sat queued for about 2.5 minutes.

Two errors are made easier to act on. A `#id` whose id contains CSS-special characters
(`#input-candidate.name-3`, as `browser_form_read` reports it) is tried as CSS first. If
that matches nothing and an element has that exact id, the tool uses `[id="…"]`. A stale
or unknown `@eN` ref returns an error that says to run `browser_snapshot` again, or to
address a form field by its label.

### Forms: questions, choice groups and autocomplete

`browser_form_read` names each field by the question it answers. Its label sources, in
order: `<label for>`, a wrapping `<label>` (only the text before the control, so a widget's
status text such as "No location found…Loading" is dropped), `aria-label`,
`aria-labelledby`, then the **question container** when the field has no label of its
own. The question container is the label, heading or `…label…`-class element that comes
before the field inside its own question block. Lever's `.application-question`, Ashby's
field-entry heading and Workable's `aria-labelledby` radiogroup are all found this way, with
no per-ATS code. Placeholders and machine names (`Type your response`, `cards[<uuid>][field0]`,
`QA_12487827`) rank last. Inline-SVG fallback text ("SVGs not supported by this browser.") and
hint, description and status nodes never reach a label. `✱` and the other asterisk look-alikes
count as required markers and are stripped like `*`.

A **checkbox group** is read as one field, like a radio group. The members are checkboxes
that share a name (Lever), or the checkboxes of one `<fieldset>`/`role=group` that holds no
other field (Ashby). It reads as
`{"kind": "checkbox-group", "label": "<question>", "options": [...], "value": [<checked>], "required": …}`.
A lone checkbox stays `kind: "checkbox"` with a boolean value. Option lists over 60 entries
are cut, and `options_more` counts the rest. `browser_select` still matches any option.

For an unanswered react-select, or a read-only select-like ARIA combobox, the read lists
`options` too. It opens the menu, reads it and closes it with Escape and blur. It never
types and never clicks an option, and the whole pass is capped at about 5 s. A
type-to-search box is skipped, because its options depend on the query. An unanswered
react-select reads `""`, never its "Select..." placeholder.

`browser_select(field, option_text)` covers every choice widget:

| Widget | `field` | What happens |
|---|---|---|
| native `<select>`, react-select, phone country picker | the field label | the option is clicked (never Enter) and the value is read back |
| radio group / checkbox group | the **question** (`"How did you hear about us?"`) | that option is ticked and `checked` is read back; an option that is already ticked is left alone (select never unticks; `browser_click` the option's own label to toggle it) |
| autocomplete text box (Lever location, Google Places, an async ARIA combobox) | the field label | see below |

Autocomplete: the CLI types the text (`fill`, then a real `End` keypress, because Lever
only searches on keyup and `fill` sends no key events). The tool then waits up to 5 s for
that field's own suggestions: an `aria-controls` listbox, a rendered `.pac-container`, or a
suggestion list in the field's container. It matches the text exactly, then by unique
prefix, then ignoring punctuation and spaces. If no suggestion appears and the text has a comma, it
retries once with the first part ("Portland, OR" → "Portland"). It still matches the full
text first and the city alone second. A unique match gets a real click (never Enter), and the
input's value is read back. The widget's own spelling of the place ("Portland, OR, USA") is
the committed value. When nothing matches, or several suggestions do, the result
lists the suggestions and the field is restored to its previous value.

`option_text` can list **alternatives** separated by `|`, for example
`"Decline to self-identify|I don't wish to answer|Prefer not to say"`. The first one that
matches wins. Matching is case-insensitive: an exact match first, then a unique prefix. No
match, or an ambiguous one, is an `Error:` that lists the options and changes nothing.
Addressing a group's question with `browser_click`, `browser_fill` or similar returns an
error that names the options and points to `browser_select`.

A CSS selector with brackets inside a quoted value
(`input[name="cards[x][field0]"][value="Yes"]`) is routed to the CLI as CSS, not treated as
a label. `browser_fill` reads the value back after filling. A value the page reformats still
passes: the same digits (phone masks, thousands separators), or the same text ignoring case
and punctuation. A blank or different value returns
`Error: <field> reads '<actual>' after filling '<text>'`.

`browser_screenshot` takes `full_page=true` (the whole scrollable page) and `annotate=true`
(numbered labels drawn on the image; label `[N]` is `@eN`, and those refs work right away).

The bundled **`web-browse` skill** teaches that loop and then defers to the CLI's own,
always-version-matched guidance (`agent-browser skills get core`), so the instructions
can't go stale against a newer binary. Two **workflows** ship as recipes —
`browse-and-extract` and `fill-form` — run on the plugin's workflow-only `browser`
subagent. `fill-form` fills by label, reads the form back with `browser_form_read`, and
**stops**: it reports the filled form and asks you to submit it (or to tell it to). It never
clicks submit.

Every tool degrades to a readable `Error: …` string instead of raising: a failed browser
action should inform the agent's loop, not crash the turn. Output is capped
(`max_response_bytes`, 200 KB by default) because page text is untrusted and unbounded.
Reading stops at the cap and the child is killed, so memory stays bounded. The reading
tools (`browser_snapshot`, `browser_get_text`, `browser_get_html`) then return the first
part, cut at a line break and marked `…[truncated: …]` with how to narrow the read. Every
other tool returns a short error.

### Bot walls and sign-in walls

After every `browser_open` the plugin reads the landed page's title, URL, status and a few
markers (one small in-page eval, `walls.py`) and asks: did a bot check, a block page or a
sign-in wall answer instead of the site? If so the tool returns

```
Error: blocked by a Cloudflare bot check at https://… — a human check is showing instead
of the page. Do not try to solve it. Ask the user to clear it in the console Browser panel,
then continue.
```

instead of the `✓ Just a moment...` it used to report as success. The `Error: blocked by`
prefix is deliberate: it keeps the `Error:` failure contract every caller already checks,
and it is one string to grep for.

| Wall | Recognised by |
|---|---|
| Cloudflare challenge / block page | `Just a moment...` / `Attention Required!` titles, `#challenge-running` and `/cdn-cgi/challenge-platform/` markup, on a near-empty page |
| "Checking your browser" pages | that text (DDoS-Guard, Sucuri, …) on a near-empty page |
| DataDome · PerimeterX · Akamai | a `captcha-delivery.com` frame · `#px-captcha` / "Press & Hold" · "Access Denied … Reference #" |
| Google unusual traffic | `google.*/sorry/` or its "unusual traffic" text |
| CAPTCHA pages | a visible reCAPTCHA / hCaptcha / Turnstile frame on a near-empty page; Amazon's robot check; Imperva's "Pardon Our Interruption" |
| Refused requests | HTTP 403 / 429; Chrome's `ERR_BLOCKED_BY_CLIENT` |
| Sign-in walls | a redirect onto a login URL, or a short page dominated by a password field titled "Sign in" |

A JS challenge often clears itself, so a Cloudflare / browser-check wall is re-probed twice
over ~5 s first; one that clears reports the page it cleared to. False positives are kept
low on purpose: a page that merely mentions "sign in" or carries a contact-form CAPTCHA is
not a wall, and opening a login page on purpose is not a wall either (being redirected to
one is). Turn the check off with `detect_walls: false`. Another plugin can reuse the
classifier: `walls.PROBE_JS` → `walls.check(eval_output, requested_url)`.

### Page content is marked as untrusted

With `content_boundaries` on (the default), the output of `browser_snapshot`,
`browser_get_text`, `browser_get_html` and `browser_form_read` reaches the
model inside the CLI's own markers:

```
--- AGENT_BROWSER_PAGE_CONTENT nonce=2585bb94… origin=https://example.com/ ---
…page text…
--- END_AGENT_BROWSER_PAGE_CONTENT nonce=2585bb94… ---
```

The nonce is random per CLI call, so a page can't forge the closing line. The flag is passed
**per call**, only on those four tools — never through `AGENT_BROWSER_CONTENT_BOUNDARIES`,
because a wrapped `eval` is no longer JSON and every form tool that parses one internally
fails. `browser_eval` is for the same reason left **unwrapped**: it returns the result of the
caller's *own* script (commonly JSON a caller then parses — design-system's `SITE_PROBE_JS`,
or any `json.loads`), not page prose the tool chose to surface, so wrapping it would break
every parsing consumer. To read untrusted page text, use `browser_get_text`, which stays
bounded. `browser_form_read` parses inside the markers and re-wraps its result in the same
ones. A consumer that parses a tool result (say, the `browser_form_read` JSON) takes the
text between the markers — `boundaries.strip()` does that. An output cut at the byte cap is closed
with its own end marker before the `…[truncated: …]` note, so the note is never inside the
page data.

### Screenshots and PDFs are fenced

`browser_screenshot` (PNG) and `browser_pdf` (Chrome's print-to-PDF) both return the
**absolute path** of the file they wrote. Pass a filename or a relative path — files land
inside the plugin's own per-instance capture directory, and an absolute path outside it is
**refused, not redirected**. That's what the manifest's `filesystem: scoped` capability
means here: a page that says "save a screenshot to `~/.ssh/authorized_keys`" cannot pick
the target.

Three details worth knowing:

- **Leave the path blank** and the file is named for you (`page-20260911-174233-7-9f3a2c.pdf`).
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

### Consequential actions ask you first

The agent can't submit a form, place an order, send a message or delete something in the
browser without your approval. Before a click, an Enter/Space key press, or typing a newline,
the plugin works out what the action will **actually hit** — and only then decides — so it
doesn't matter whether the agent named the button by a `@eN` ref, a CSS selector or a label:

- a CSS selector or label is looked up in the page;
- a `@eN` ref is the CLI's, so the plugin asks the CLI for its box and names the element at
  the box's centre, which is where the CLI's click lands. When that doesn't line up (a ref
  inside an iframe, an overlay, off-screen), it falls back to the CLI's text and `type` for
  the ref.

An action falls in the **`submit`** category when it submits a form (a button in a form, an
`input[type=submit]`, Enter in a form field) or when the control's name commits something —
*Submit*, *Send*, *Place order*, *Pay*, *Confirm*, *Delete*, *Publish*, *Sign up*, and so on —
even with no form behind it. It is **not** `submit` when the name or the form says it's
navigation or a query: a job board's *Apply* / *Apply now* (which opens the application),
*Apply filters*, *Search*, *Sign in*, *Next*, a search form, or any GET form.

When a gated action comes up, the chat shows an approval card in plain words — *Submit the form
on boards.greenhouse.io — button “Submit application”* — with **Allow** / **Don't allow**.
Allow runs the action once. Don't allow returns an error that tells the agent not to retry.
If the page changes while the card is open, so the target is no longer what you approved,
nothing happens and the agent has to ask again.

A card can only be answered in an operator chat turn. A delegated subagent (including a
workflow step), or a scheduled, inbox, background or remote (A2A) turn, gets an immediate
refusal instead of parking on a card nobody will see. The agent is told to leave the page as it
is and hand it to you; a subagent is told to end its step and report that the action is waiting
for you, so the lead (on your chat turn) can take it and show you the card. A `/bypass` turn doesn't skip the gate either.

`confirm_actions` picks the categories:

| Value | Gated |
|---|---|
| *(not set)* | `submit` — the default |
| `submit` | form submissions and committing controls |
| `eval` | every `browser_eval` |
| `upload` | every `browser_upload` (the file is already fenced to the capture directory) |
| `download` | a click on a link with a `download` attribute |
| `all` | all of the above |
| `""` / `none` | nothing — the gate is off and costs no extra CLI calls |

Combine them with commas (`submit,eval`). The value used to be passed to the CLI as
`--confirm-actions`, which gated whole verbs and waited for a confirmation nothing ever sent,
so a gated action could only fail. The plugin handles it now and passes nothing to the CLI.
Unknown words are ignored with a warning.

Resolving a target costs one extra CLI call for a CSS selector or label, and two for a ref
(four when the ref falls back). It happens only for a verb that a configured category can apply
to. A `press` of any key but Enter or Space, a `type` with no newline, and an `eval` that isn't
gated cost nothing.

**For plugin authors.** The gate offers two [plugin services](/adr/0116-local-first-data-analyst-duckdb-and-vega-lite-charts):

- `agent_browser.add_gate_policy(fn, categories=("submit",), name="")` layers your own rule
  on top of the gate. `fn(action)` gets plain data: `tool`, `verb`, `selector`, `key`,
  `page_url`, the resolved `target` (tag, type, role, name, `submits`, form method/action,
  href, …), core's `verdict` (`categories`, `reason`, `description`), and `gated`. It returns
  `None` or `{"decision": "allow" | "ask" | "deny", "reason": …}`. Deny beats ask, and ask beats
  allow. A registered policy also makes targets resolve when the operator has the gate off. It
  returns a function that removes the policy.
- `agent_browser.describe_target(target)` names a resolved target in the card's words.

## The Browser panel

The plugin ships a console view ([ADR 0026](/adr/0026-plugin-contributed-console-surfaces)) that is a
**fully drivable viewport**, not a screenshot poll: a live CDP **screencast** (event-driven
JPEG frames) painted onto a `<canvas>`, with your mouse, keyboard and scroll forwarded
back into the page as `Input.dispatch*`. With the default `session_scope: shared`, you and the agent are in the same browser —
take over a login wall by hand, then let it continue. With `session_scope: per_session`,
each chat drives its own browser (see [Which browser](#which-browser-an-agent-drives)). The panel still follows the
agent's main browser, so it may remain empty while the agent browses; it cannot display or
control those per-chat pages. Following a per-chat session is future work.

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

## Which browser an agent drives

**One browser per agent.** The CLI keeps a browser per *session name* — its own daemon,
its own Chrome, its own cookie jar — and calls it `default` when nobody names one. Every
protoAgent instance on a machine (each fleet member, the dev sandbox, the desktop hub) is
its own server process, so before the plugin named the session itself, every agent on the
machine drove that one `default` Chrome: one agent saw another's page mid-task, parallel
form fills crossed between agents, and every agent shared every other agent's signed-in
cookies.

The plugin now names the session from the instance: `pa-<instance>-<hash>`, where
`<instance>` is the readable instance id (`jobcoach-2e97`, `dev`, or `main` for an unscoped
default/desktop hub) and `<hash>` comes from the instance's data root. The same root always
gets the same browser, and two instances never share one. The tools, the panel's toolbar and
its live view all pass that one name to every CLI call they make, so they always agree.

- **`session_scope: per_session`** goes one step further: **one browser per chat**, named
  `pa-<instance>-<hash>-<chat hash>` — namespaced by the instance, so two agents' chats can't
  meet either. A call with no chat behind it uses the agent's main browser.
- **`AGENT_BROWSER_SESSION`**, set in the server's environment, still wins: it names the
  agent's main browser outright (two instances given the same value share it again).
- `agent-browser session list` shows which browsers are running.

**Idle browsers shut down.** Each per-chat browser exits — Chrome and all — after
`idle_timeout_min` (default 30) minutes with no browser command, so chats that finished
don't each keep a Chrome running until the next reboot. The agent's main browser has its
own `main_idle_timeout_min`, **off by default**: your signed-in tabs live there, and the
panel's live view talks to Chrome directly, so watching it doesn't count as activity — a
timeout would close the browser under you. This is the CLI's own reaper
(`AGENT_BROWSER_IDLE_TIMEOUT_MS`), which a browser reads when it **starts**: a change
applies to browsers started afterwards. A chat that comes back after its browser was reaped
gets a fresh, empty one.

**A `profile` directory backs one browser at a time.** Chrome locks a profile directory
while it uses it; a second Chrome on the same directory refuses to start. So with
`per_session`, only one chat at a time can use a configured `profile` (the next chat's
`browser_open` returns an error that says so). The same applies when two agents are pointed
at the same directory. A Chrome profile *name* (`Default`, `Profile 1`) is copied per launch
by the CLI and never collides.

## Settings

Editable in **Settings ▸ Plugins ▸ Agent Browser**, or under `agent_browser:` in
`langgraph-config.yaml`. Blank / `0` / `false` means "the CLI's own default".

| Setting | What it does |
|---|---|
| `binary` | The `agent-browser` CLI. Override with an absolute path when PATH isn't enough. |
| `session_scope` | `shared` (default) uses one browser for the agent across chats; `per_session` gives each chat its own. The Browser panel and screencast follow the agent's main browser. Either way, other agents on the machine have their own. |
| `idle_timeout_min` | Minutes before an idle per-chat browser shuts down (default 30; `0` = never). |
| `main_idle_timeout_min` | The same for the agent's main browser (default `0` = never). |
| `cli_autofetch` | With no CLI on PATH, download the pinned, checksum-verified release on first use (default on). The banner's Download button works either way. |
| `timeout_s` · `max_response_bytes` | Per-command subprocess timeout; the aggregate stdout+stderr byte cap. |
| `home_url` | The page the panel opens to. Set it and the panel auto-opens it when nothing is open; blank gives a Start button. |
| `stream_quality` | Panel JPEG quality (1–100). |
| `headed` | Show a real browser window instead of running headless. |
| `profile` | A browser profile directory — persisted auth/cookies across sessions. One browser can use it at a time. |
| `device` | Device emulation, e.g. `iPhone 16 Pro`. |
| `allowed_domains` | A navigable-domain allowlist. The tightest lever you have on where the agent can go. |
| `confirm_actions` | Which consequential actions need your approval first: `submit` (the default when unset), `eval`, `upload`, `download`, `all`, or blank/`none` for off. See [Consequential actions ask you first](#consequential-actions-ask-you-first). |
| `max_output` | Cap the page text returned to the model (the CLI's own LLM-safety knob). |
| `content_boundaries` | Wrap page-derived output in untrusted-content markers (default on). See [Page content is marked as untrusted](#page-content-is-marked-as-untrusted). |
| `detect_walls` | Report a bot check, block page or sign-in wall after `browser_open` as `Error: blocked by …` (default on). See [Bot walls and sign-in walls](#bot-walls-and-sign-in-walls). |
| `stealth` · `user_agent` · `browser_args` | Anti-detection and raw Chrome launch args — see below. |

Launch options apply when the **session launches** (the first `open`). A session already
running keeps its setup until it is closed and reopened. A session started from the panel's
Start button gets the same flags as one the agent opens.

### Pages that block automation

Google, Reddit and Cloudflare-fronted sites detect and refuse automated browsers. The
levers, weakest to strongest:

- `stealth: true` (off by default) drops the `navigator.webdriver` automation flag.
  When headless it also swaps the give-away `HeadlessChrome` User-Agent for a real desktop
  one. That User-Agent claims the **installed** Chrome's major version, as the CLI's doctor
  reports it, in Chrome's own reduced form (`Chrome/151.0.0.0`), because a UA naming a
  different Chrome than the one running the page is a tell of its own. If the version
  can't be read, it falls back to `Chrome/149.0.0.0`;
- `user_agent` / `browser_args` — the same thing by hand, for fine control;
- `headed: true` **plus** a logged-in Chrome `profile` — much the most reliable, because
  it is a real window with real session state.

No setting defeats detection entirely, and none of them change the fact that you are
responsible for what the agent does on a site.

## For plugin authors: the browser client

A plugin that needs a browser of its own (a price checker, a scraper) shouldn't shell out
to `agent-browser` itself. Get the browser client through the
[`agent_browser.client` service](/adr/0116-local-first-data-analyst-duckdb-and-vega-lite-charts)
instead. It runs every command through the same code as the browser tools: the byte cap,
the process-group kill on a timeout, CLI resolution (PATH, then `binary`, then the pinned
download on first use) and the guard against option-shaped arguments. Results come back
typed instead of as strings.

```python
from graph import sdk

make = getattr(sdk, "service", lambda _name: None)("agent_browser.client")
if make is None:
    ...  # agent_browser is off, or the core is older than the client: fall back or say so
browser = make(session="shop", profile="~/.protoagent/shop-profile", headed=True,
               own_tab="shoptab", min_interval_s=1.5, nav_timeout_s=30)

r = browser.open("https://shop.example/search?q=lamp")
if r.kind == "wall":        # a bot check, block page or sign-in wall: r.wall.kind / r.wall.label
    return f"The site is showing {r.wall.label}. Clear it in the browser window, then retry."
if r.kind == "timeout":     # the browser didn't answer (a stuck tab, a site not answering), not a wall
    return r.error
browser.wait(selector="li.result", timeout_s=10)  # a timeout here is an answer, not a crash
rows = browser.eval_json("JSON.stringify([...document.querySelectorAll('li.result')].map(e => e.innerText))").value
```

Build the client once, at the top of your tool builder, and share it between your tools. The
pacing clock lives on the instance. Every method is **synchronous and thread-safe**. Call it
from a sync tool (LangChain runs those in a worker thread), or wrap it in
`asyncio.to_thread`. Never call it on the event loop.

**Every method returns a `Result`.** `kind` is a plain string, so you can branch on it
without importing anything:

| `kind` | Meaning |
|---|---|
| `ok` | It worked. `text` holds the CLI output; `value` holds the parsed payload (`open`: `{url, title}`, `eval_json`: the script's result, `tabs`: the tab list, `form_read`: the field list). |
| `timeout` | No answer in time. The browser or its tab is stuck, or the site is not answering. Not a wall. |
| `wall` | A bot check, block page or sign-in wall answered instead of the page. `wall.kind`, `wall.label`, `wall.url`, `wall.transient`. |
| `cli_error` | The CLI ran and failed: no such element, a profile already in use, Chrome refused, a `form_read` scope that matched nothing. |
| `script_error` | `eval_json` / `form_read`: the page script threw an error, or answered something unreadable. |
| `unavailable` | There is no CLI to run: not installed, still downloading, or it can't start. |
| `bad_argument` | Refused before anything ran: an option-shaped operand, a malformed wait. |
| `too_large` | The output passed the byte cap. (`get_text` and `snapshot` return a cut first part with `truncated=True` instead.) |

`error` is a plain sentence that says what to do next, safe to show verbatim. Methods
don't raise for an expected failure. If you'd rather use exceptions,
`r.raise_for_error()` raises `browser.Error` with the same `kind`.

**Methods:** `open(url, check_walls=True, recover=True)`; `wait(selector= | text= | url= | load= | js=, timeout_s=10)`;
`eval_json(script)`; `form_read(scope="")`; `get_text(selector="body", content_boundaries=False)`;
`snapshot(interactive=True, compact=True, scope="", depth=0)`; `check_wall(requested_url="")`;
`get_url()`; `tabs()`; `focus_tab(label="")`; `close_panels()`; `close()`; and `hold()`, a
context manager that keeps a multi-step sequence together.

**Reading a form as data.** `form_read(scope="")` runs the same in-page reader as
`browser_form_read`, on this client's session, and returns the fields as data instead of a
string for the model. That includes opening each unanswered select-like combobox to list its
options and closing it again without committing anything. `scope` is a CSS selector or a
visible container label (not a `@eN` ref); blank reads the whole page. `value` is a list of
dicts in document order, the shape the tool renders:

| Key | |
|---|---|
| `label` · `kind` · `name` · `id` · `required` | What the field is. `kind` is `text`, `textarea`, `email`, `tel`, `number`, `native-select`, `combobox`, `checkbox`, `radio-group`, `checkbox-group`, `file` or `other`. |
| `value` | The committed value: a combobox's rendered selection (not half-typed search text), a radio group's checked label, a checkbox group's checked labels (a list), a file input's attached file name. |
| `options` · `options_more` | Where the field has options. The list stops at 60; `options_more` counts the rest. |

There are no content-boundary markers in it. It's for your code, not the model, so a plugin
can check a form against the real page instead of trusting a read-back the model passed in.
(An eval caught the model shortening one: 7.3 KB from the browser arrived as 3.8 KB in the
tool call.) An empty form is `ok` with `[]`. Past the byte cap it's `too_large`, never half a
list.

**Acting on the agent's own browser.** `make(session=...)` is always a browser of your own.
To read the page the agent's `browser_*` tools are driving in the current chat (say, to check
what it just filled in), build the client from inside your tool call with
`make.for_agent_session(state)`, passing the tool's injected graph state:

```python
from typing import Annotated, Any
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

@tool
def verify_application(state: Annotated[Any, InjectedState] = None) -> str:
    """Check the filled form against the page."""
    browser = make.for_agent_session(state)   # the session browser_fill just used
    r = browser.form_read()
    if not r.ok:
        return f"Error: {r.error}"
    fields = r.value                           # list of dicts, see above
    ...
```

It picks the session the same way the tools do. Under `session_scope: shared` (the default)
that's the instance's main session (`AGENT_BROWSER_SESSION`, else `pa-<instance>-<hash>`).
Under `per_session` it's the name derived from `state["session_id"]`. Pass `state`:
`current_session_id()` is empty inside a tool body, so under `per_session` a call with no
`state` and no `session_id=` raises `ValueError` rather than reading some other chat's page.
The client takes the same per-session guard as the tools, so a read never lands in the middle
of a `browser_fill`. It sends the browser plugin's own launch flags (profile, headed, …) so it
can't relaunch the agent's Chrome with different ones, and its `close()` is refused. Keep the
tool **sync** (or use `asyncio.to_thread`): a client call blocking the event loop would wait
forever behind a browser tool that needs that loop to finish. `for_agent_session` takes
`session_id=`, `timeout_s` and `nav_timeout_s`. The other `make` options belong to a browser
of your own and don't apply.

**Options**, all keyword arguments to `make(...)`:

| Option | Default | |
|---|---|---|
| `session` | required | Your browser's name: letters, digits, `-`, `_`, up to 24 characters. |
| `instance_scoped` | `True` | Prefix it with this instance's name (`pa-<instance>-<hash>-<session>`) so two agents on one machine never share a Chrome. Set `False` to keep a fixed name that an operator has already signed in under. |
| `profile` · `headed` · `user_agent` · `browser_args` | none | Launch options: a Chrome profile directory or name, a visible window, a User-Agent, extra Chrome flags (each `--name[=value]`). The client adds none of its own: no stealth, no anti-throttle flags. |
| `own_tab` | none | A tab label. The client navigates only that tab (see below). |
| `timeout_s` · `nav_timeout_s` | 30 · 45 | Per command, and per `open`. |
| `min_interval_s` | 0 | The minimum gap between navigations, so you don't hit a site faster than a person would. |
| `idle_timeout_min` | 0 (never) | Close the browser after this long idle. It applies when the client starts the browser. |

What it handles for you:

- **Launch options go with every `open(url)`**, and the client never runs `open` without a
  URL. The CLI keeps one set of launch options per browser and settles them on each launch
  request. `open <url>` with flags sends one request: same options reuse the browser,
  different options relaunch it on the same profile, and no browser starts one. A URL-less
  `open` with flags sends that request plus a second one with no profile, and Chrome
  relaunches onto a temp profile, so your sign-in is gone. `open("")` is refused. Use
  `open("about:blank")` for a blank page.
- **Waits always carry a timeout** (10 s by default, 25 s at most). The CLI gives up
  reading the daemon's reply after 30 s and **re-sends the command, up to five times**. A
  wait with the daemon's default 30 s timeout that never succeeds therefore costs about
  2.5 minutes.
- **A navigation that hangs is cut off after `nav_timeout_s`**, so the CLI doesn't re-send it
  five times. The client then recovers once. With an `own_tab`, it replaces that tab first.
  If that also times out, it reports `timeout` and says it is not a wall.
- **`own_tab`.** agent-browser runs every command on the active tab, and makes any new
  target active. In a headed window you share with the operator, that can be Chrome's
  Gemini side panel (navigating it fails with `ERR_BLOCKED_BY_CLIENT`) or one of the
  operator's own pages. With `own_tab`, every navigation first switches to the client's
  labelled tab. A panel that grabs the tab mid-navigation is closed and the navigation is
  retried once. A tab of the client's own that won't take focus is replaced. The only things
  ever closed are Chrome's own panels and the client's own tab, never a page of the
  operator's.
- **The same lock as the tools.** If your `session` is the agent's own browser, a client
  sequence and a `browser_fill` can't interleave. On a session of your own, the client never
  waits for the tools.

## Where things live

| File | What |
|---|---|
| `tools.py` | the 21 tools — subprocess wrappers, the byte cap, the fenced captures, the gate's hooks |
| `runner.py` | the one way a CLI command runs (byte cap, process-group kill, bounded joins) and the per-session guard, shared by the tools and the client |
| `client.py` | the browser client for other plugins (the `agent_browser.client` service) |
| `storage.py` | the capture write fence |
| `preflight.py` | the setup-gap probe (which CLI resolves, does it have a Chrome) and the banners' buttons |
| `cli_fetch.py` | the pinned, SHA-256-verified CLI download: the pins, the atomic install, the cache |
| `chrome_install.py` | `agent-browser install`, run only from the Install Chrome button |
| `setup_steps.py` | what the Download agent-browser / Install Chrome buttons do |
| `browser_panel.py` | the panel page + its gated nav / ticket / stream routes |
| `browser_stream.py` | the CDP bridge — frames out, input in, resize + nav re-arm, the WS ticket |
| `runtime.py` | what the tools and the panel share: the launch flags, the session name and its env, the idle reaper |
| `gate.py` | the action gate: target probe, `submit`/`eval`/`upload`/`download` policy, approval card, plugin policies |
| `walls.py` | the bot-wall / sign-in-wall probe and classifier (`browser_open`) |
| `boundaries.py` | the untrusted-content markers: parse, re-wrap, strip |
| `skills/` · `workflows/` | the `web-browse` skill and the two recipes (run on the `browser` subagent) |

See also: [Plugins](/guides/plugins) · [Building a plugin view](/guides/building-react-plugin-views)
· [Sandboxing & egress](/guides/sandboxing).
