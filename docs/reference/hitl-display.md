# HITL display contract

A producer that already pauses for human input may attach `display` to the interrupt
payload. Keep the complete decision context in `description`/`detail` (approval),
`question`, or the existing form prose: clients may ignore display, and validation
may discard it. The host's existing actions alone answer the pause. A display has
no authority to resume, submit a form, start a chat turn, or open a link.

## Built-in table

```json
{
  "display": {
    "component": "table",
    "props": {
      "columns": ["Field", "Value"],
      "rows": [["Full name", "Ada Lovelace"], ["Resume", "resume.pdf  (upload)"]]
    }
  }
}
```

This delivery supports only the built-in read-only `table`. Headers are strings;
rows are arrays with exactly as many cells as headers. Cells may be strings,
finite numbers, booleans, or null. Null renders empty; other values render as
literal text. HTML, Markdown, URLs, and strings mentioning `openLink` do not gain
behavior. No artifact iframe, send/openLink bridge, form controls, callbacks, or
arbitrary component registry participates. The table uses the console's theme and
horizontal overflow container; the host card retains its scrolling and actions.

The fixed server limits in `src/server/hitl_display.py` are:

| Limit | Maximum |
| --- | ---: |
| Columns | 16 |
| Body rows | 100 |
| Characters per header | 200 Unicode code points |
| Characters per string cell | 1,000 Unicode code points |
| Serialized sanitized display | 65,536 UTF-8 bytes |

Byte size uses Python `json.dumps(..., ensure_ascii=False)` with its default
separator spaces, over the sanitized `{component, props}` object. Extra properties
are discarded, never passed to DOM or design-system components. Empty columns and
rows are allowed if row widths match. Nested objects/arrays, non-finite numbers,
ragged rows, unknown components, and any exceeded bound discard the **whole display**;
there is no truncation that could hide part of an approval. The console also
validates legacy/unvalidated input defensively.

`server.chat._interrupt_payload` preserves recognized approval/form/question fields
and validates only the optional display. Missing, malformed, oversized, or
unsupported display leaves prose, options, and resume values intact. Producers
must therefore provide a complete text fallback even when the table looks better.

## Implemented producer: careercoach

The merged [`_submit_approval_card` producer](https://github.com/protoLabsAI/careercoach-plugin/blob/95fc87f921b890238ac444aa22a40929193815bd/__init__.py#L1205)
returns `kind: "approval"`, `type: "careercoach_submit_approval"`, and title
`Approve submitting this job application?`. Its description includes role and
company and the existing one-click, 120-second authorization wording. Its
`detail` contains one `  Field -> Value` line per planned answer; `prompt` retains
the same information for older clients. `company`, `role`, `session_id`, and
`fields` remain in the producer payload.

The table has `Field`/`Value` columns. Non-dictionary and `skip` rows are excluded
from both the table and fallback; upload values receive exactly `  (upload)`.
The host buttons retain the old values: **Approve submit** sends `approve`, and
**Decline** sends `decline`. Display does not change careercoach's submit grant or
verification enforcement. No careercoach code is changed by this console proof.

The browser fixture in `apps/web/e2e/hitl-display.spec.ts` is the complete output
of that pinned producer executed with fill, upload, hostile literal text, and skip
rows (only its relative constants import was stubbed). It is not a separately
designed approximation of the card. It checks visible table text, absence of skipped
fields and executable elements, zero chat sends on cell clicks, exact resume
values and task identity, and absent/invalid/unsupported fallback actions. A separate 16-column stress
variant checks horizontal scrolling within the narrow phone card.

## Browser evidence

Run from the repository root after `npm ci`:

```sh
npm run test:unit --workspace @protoagent/web
npm run test:e2e --workspace @protoagent/web -- hitl-display.spec.ts
python scripts/gate.py
```

The scenario runs desktop and a 320-pixel phone with touch/mobile emulation, in
light and dark themes. It attaches eight PNG screenshots through Playwright
`testInfo.attach`, one for each viewport/theme/decision combination. They are
runtime artifacts under `apps/web/test-results`, not committed binaries;
use Playwright's HTML reporter (`--reporter=html`) to review attachments in
`apps/web/playwright-report/index.html`.
This is Chromium browser evidence, not a claim of native WKWebView/WebView2 testing.

The authority boundary follows [ADR 0118](../adr/0118-interactive-answers-inline-artifacts-and-frame-components.md):
HITL answers the pause; display only presents data.
