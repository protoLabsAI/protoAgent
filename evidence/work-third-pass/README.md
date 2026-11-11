# Work third-pass review evidence

Draft for Josh, with `merge-hold` and GitHub auto-merge disabled. Refs
protoLabsAI/protoContent#673, protoLabsAI/protoContent#674,
protoLabsAI/protoContent#675. No agent merge.

Base: fresh `origin/main` at `877ca4780d5c221b090eddb2410623919b4fae33`
(merged #5083). The all-chat behavior is retained. No dependency changes.
The #5079 header clearance and `docs/design/work-view.md` are unchanged.

## Screenshots

These are real mock-backend captures from the implementation, not the approved
preview. The six PNGs are included as repository file artifacts for the owning
loop to commit/publish with the draft PR. This edit-only run did not push or
upload them externally. Each capture is also attached by Playwright.

| View | Viewport | Artifact |
| --- | --- | --- |
| Rail Overview | 1440 × 900 | [Rail](01-rail-overview-1440x900.png) |
| Window Overview, nothing selected | 1440 × 900 | [List](02-window-overview-1440x900.png) |
| Window item inspector | 1440 × 900 | [Inspector](03-window-inspector-1440x900.png) |
| Window empty Decisions, no pending | 1440 × 900 | [Empty Decisions](04-window-empty-decisions-1440x900.png) |
| Phone list | 390 × 844 | [Phone list](05-phone-list-390x844.png) |
| Phone item detail | 390 × 844 | [Phone detail](06-phone-detail-390x844.png) |

## Reproduction

From the repository root, using its locked npm workspace dependencies:

```sh
npm ci
npm run build --workspace @protoagent/web
WORK_SCREENSHOT_DIR="$PWD/evidence/work-third-pass" npx --prefix apps/web playwright test --config apps/web/playwright.config.ts apps/web/e2e/work-view.spec.ts --workers=2 --project=chromium
npx --prefix apps/web playwright test --config apps/web/playwright.config.ts apps/web/e2e/work-mobile-layout.spec.ts --workers=2
npx --prefix apps/web playwright test --config apps/web/playwright.config.ts apps/web/e2e/work-palette.spec.ts --workers=2 --project=chromium
npx --prefix apps/web playwright test --config apps/web/playwright.config.ts apps/web/e2e/surface-context-menu.spec.ts --workers=2 --project=chromium
```

Run suites sequentially. Playwright starts `e2e/mock-server.mjs`; no model or
production backend is used. Browser binaries must already be installed.

Related unit coverage, from `apps/web`:

```sh
npx vitest related src/work/WorkView.tsx src/work/WorkOverview.tsx src/work/WorkDetail.tsx src/work/sections/*.tsx src/shell/SurfaceHost.tsx src/decisions/DecisionCard.tsx src/chat/DirectiveEntry.tsx src/chat/DirectiveDrawer.tsx src/app/BackgroundJobs.tsx src/work/WorkDependency.test.ts --run --maxWorkers=2
```

## Results

- Production build and TypeScript: pass.
- Related Vitest: 51 files passed; 920 tests passed, 8 skipped.
- Work view: 6 Chromium tests passed, including all six screenshots, exact heading
  names, selection-only detail, Escape from the Dialog header, nested menu and
  confirmation handling, opener focus, phone action sizes, and reflow at a
  360 CSS-pixel viewport (1440px at 400% zoom).
- Work mobile layout: 24 tests passed across Chromium and WebKit, light and dark.
  Includes all nine picker sections, 390/560px phone push navigation, actual md
  buttons, scroll/focus preservation, and container-only breakpoint changes.
- Work palette: 8 tests passed.
- Surface context menu: 2 tests passed.
- Matching published `ui`/`ui-css` provenance gate: passed (0.73.0); package ranges
  and lockfile are unchanged.
- `python3 scripts/gate.py`: ruff passed; stopped because `lint-imports` is not
  installed. Later gate steps were not run. `python` itself is absent here.
- Official `ds_check`: unavailable in this session (no tool or executable).
  This is **not** reported as a pass. A separate static audit of the three changed
  CSS files found no unresolved `--pl-*` tokens and no literal pixel/color values.
- `git diff --check`: passed.

The WebKit container-resize test caught a delayed zero-scroll event after a
hidden list became visible. Work now ignores scroll events with dimensions that
have not yet been reconciled by ResizeObserver; the existing resize test and a
new unit regression both pass.

Static CSS audit, from the repository root:

```sh
python3 - <<'PY'
from pathlib import Path
import re
known = set(re.findall(r'(--pl-[\w-]+)\s*:', Path('apps/web/node_modules/@protolabsai/design/dist/tokens.css').read_text()))
for name in ['work-view.css', 'work-row.css', 'work-detail.css']:
    css = (Path('apps/web/src/work') / name).read_text()
    assert not set(re.findall(r'var\((--pl-[\w-]+)', css)) - known
    assert not re.findall(r'\b\d+(?:\.\d+)?px\b|#[0-9a-fA-F]{3,8}\b', css)
    print(name, 'PASS')
PY
```
