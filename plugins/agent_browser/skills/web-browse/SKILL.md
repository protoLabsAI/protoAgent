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
  - browser_form_read
  - browser_click
  - browser_fill
  - browser_type
  - browser_select
  - browser_upload
  - browser_get_text
  - browser_press
  - browser_eval
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

Text that starts with a dash and a letter (like `--foo`) is refused by `browser_fill`
and `browser_type`: the CLI would read it as one of its own options. Set such a value
with `browser_eval` instead. Negative numbers, `-$50.00`, list bullets and a lone `-`
are fine.

## Filling forms

Real application forms (Greenhouse, Ashby, Workday) re-render as you touch them, and a
plain snapshot/`@eN`/type+Enter loop fails on them — refs go stale, custom widgets eat
synthetic clicks, and comboboxes commit the highlighted row instead of the one you meant.
Fill a form like this:

1. **Read it first with `browser_form_read`.** One call lists every field in document
   order with its `label`, `kind`, `required` flag, current `value` and (for selects and
   radio groups) its `options`. This is how you learn what to fill and what to address each
   field by.
2. **Address fields by their LABEL, never by an `@eN` ref.** A snapshot ref is resolved
   once and goes stale the instant the form re-renders (a validation error, a React state
   change); a label is re-resolved in the page on every call, so it survives. `browser_fill`,
   `browser_type`, `browser_click` and the tools below all take a label (or a CSS selector).
3. **Use `browser_select` for every dropdown, combobox or country picker — never
   type-and-Enter.** It drives a native `<select>`, a react-select-style combobox and an
   intl-tel-input country picker, commits the option by CLICKING it (pressing Enter commits
   whatever row happens to be highlighted — the wrong answer), and reads the value back so a
   mismatch is a hard error rather than a silent wrong submission.
4. **For a phone field, select the COUNTRY before you fill the number.** Use
   `browser_select` on the country picker first, then `browser_fill` the national number —
   changing the country afterwards reformats or clears what you typed.
5. **For a résumé/CV upload, make the file then attach it.** `browser_pdf` writes a PDF
   into the browser's own capture directory and returns its path; hand that path straight to
   `browser_upload`, which attaches it to the file input (even when it hides behind an
   "Attach" button) and verifies the attached filename.
6. **If a click reports success but the page visibly does nothing, retry with
   `browser_click` and `js_fallback=true`.** It re-clicks the element in the page directly
   (the Greenhouse "Enter manually" button needs this) and never clicks twice when the first
   click already worked.
7. **When every field is set, read the form back with `browser_form_read` and diff each
   value against the answer you intended.** Fix any field that does not match before you
   consider the form done.

**Never click a submit/apply button unless the operator has explicitly told you to
submit.** Filling a form is not the same as sending it; leave it filled and verified, and
say so. And a **captcha or a login is a human step** — you cannot solve it. Stop, say which
page needs it, and let the operator take over.

## Capture a page as a file

`browser_screenshot` (PNG) and `browser_pdf` (Chrome's print-to-PDF) both return
the **absolute path** of the file they wrote. Pass a filename or a relative path —
or leave it blank and one is generated for you, which is what you want unless the
name matters. They always land in this plugin's own capture directory, and an
absolute path outside it is refused. Re-using a filename replaces that file only if
the new capture succeeds; a failed one leaves the previous file in place.

Capturing a blank page is refused. To print HTML you generated, open it as a
`data:text/html,…` URL (URL-encoded) or save it to a file and open its `file://`
URL — or `browser_open` with no URL and write the markup in with `browser_eval`.

`browser_pdf` is the **HTML → PDF** route. To hand the user a real PDF (a resume, a
report, an invoice): render or open the page, `browser_pdf`, then pass the returned
path to `save_file_artifact` so it appears in the Artifact panel with a Download
button. That works for a URL and for an HTML file you generated yourself — open it
with a `file://` URL.

The PDF is always **US Letter** (8.5 x 11 in): the CLI has no paper-size option and
ignores a page's CSS `@page size`, so an A4 layout still prints on Letter. Lay the
page out for Letter, and never tell the user a PDF is A4.

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
