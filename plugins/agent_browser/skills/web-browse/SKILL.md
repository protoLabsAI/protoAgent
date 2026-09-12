---
name: web-browse
description: >-
  Open a website, fill a form, click buttons, take a screenshot, print a page
  to PDF, scrape or extract page content, test a web app, log in, or automate
  any browser interaction.  Trigger when the user asks you to browse, visit,
  navigate, interact with, or automate a web page — or to turn a page or an
  HTML artifact into a PDF.
tools:
  - browser_open
  - browser_snapshot
  - browser_click
  - browser_fill
  - browser_type
  - browser_get_text
  - browser_press
  - browser_screenshot
  - browser_pdf
  - browser_close
---

# web-browse

Drive a real browser through the `agent-browser` CLI.  Every task follows the
same four-step loop:

1. **Open** — `browser_open <url>` to navigate to the target page.
2. **Snapshot** — `browser_snapshot` to get the accessibility tree with
   compact `@eN` element refs (e.g. `@e7`).
3. **Act** — use an `@eN` ref (or a CSS selector) with `browser_click`,
   `browser_fill`, `browser_type`, `browser_press`, etc.
4. **Verify** — run `browser_snapshot` again (or `browser_get_text`) to
   confirm the action succeeded; repeat steps 3–4 until done.

When finished, call `browser_close` to free the browser session.

## Capture a page as a file

`browser_screenshot` (PNG) and `browser_pdf` (Chrome's print-to-PDF) both return
the **absolute path** of the file they wrote. Pass a filename or a relative path —
or leave it blank and one is generated for you, which is what you want unless the
name matters. They always land in this plugin's own capture directory, and an
absolute path outside it is refused. If a capture reports an error saying no file
was written, call `browser_open` first — there was no page to capture.

`browser_pdf` is the **HTML → PDF** route. To hand the user a real PDF (a resume, a
report, an invoice): render or open the page, `browser_pdf`, then pass the returned
path to `save_file_artifact` so it appears in the Artifact panel with a Download
button. That works for a URL and for an HTML file you generated yourself — open it
with a `file://` URL.

For the always-current usage — workflows, common patterns, troubleshooting, and
(with `--full`) the complete command reference and templates — load it from the
CLI, which serves skill content matched to the installed version so the
instructions never go stale:

```
agent-browser skills get core          # start here — workflows, patterns, troubleshooting
agent-browser skills get core --full   # + full command reference and templates
```

There are also specialized skills (e.g. Electron apps, Slack, cloud browsers) —
list them with `agent-browser skills list`.
