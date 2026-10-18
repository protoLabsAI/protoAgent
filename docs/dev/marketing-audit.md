# Marketing audit — October 2026

Tracker: [#4339](https://github.com/protoLabsAI/protoAgent/issues/4339).
Implementation: [PR #4343](https://github.com/protoLabsAI/protoAgent/pull/4343).
Baseline: `99bc8ac4a7f0b177fa273b6e507a895a075e7ba3`; live site reviewed
2026-10-18 at <https://agent.protolabs.studio/>. Audience priority: app users,
then explicit builder and operator paths. Preserve the existing design system and URLs.

## Route dispositions

| Surface | Finding | Change / acceptance |
| --- | --- | --- |
| `/` | Template/clone-first positioning, incomplete pip recipe, Linux marked testing, stale internal-deployment anecdotes, no useful product visual | Lead with app outcomes and desktop setup; add conceptual product map, audience paths and model/data FAQ. No unsupported size, live-customer, or benchmark claims. |
| `/features` | Repeated lean-core pitch; memory described as always embedding-backed; default sandbox overclaim; current files/devices/automation controls omitted | Group app work, automation, extension and operation; link each capability to canonical docs and state relevant limits. |
| `/use-cases` (new) | No path from a visitor's task to the right setup | Four bounded examples: documents, codebase work, recurring tasks, separate agents; starter roles derive from the active archetype catalog. Label example requests and name prerequisites; no fabricated outcome evidence. |
| `/download` | Stale fixed app-size/runtime claims; incomplete source setup; macOS-only no-JS fallback; newsletter overstates automatic updates for all formats | Preserve live release resolver and all installer URLs. Point package/source users to complete setup; explain optional runtimes and offer all platforms without JS. |
| `/plugins` | Installation guidance aimed at source checkouts; all first-party entries described as bundled; edit link points to generated JSON | Use the packaged CLI and Settings path; distinguish bundled and external packages; link the directory's owning YAML. Preserve dynamic discovery, filters, and status rules. |
| `/roadmap` | Phone scope does not distinguish existing QR access from future typed-code/tablet work | Update ROADMAP.md and regenerate JSON. Link planned work to #4336; retain the two pending upstream listings after checking both PRs are still open. |
| `/changelog` | Release data current (v0.208.0); metadata still calls product a template | Keep generated/release history intact; update description only. |
| Shared chrome/SEO | Top nav mixes first-use and release-history paths; no canonical link or skip link; social card has old positioning | Add Use cases, keep roadmap/changelog in footer, add canonical and keyboard skip link; regenerate social card from an editable source. |

## Claim evidence

- Install/model paths: `docs/tutorials/first-agent.md`, `docs/guides/model-connections.md`,
  `src/tools/onboard_tools.py`, `apps/desktop/src-tauri/tauri.conf.json`.
- Files/context: `docs/guides/documents-and-files.md`, `docs/guides/python-runtime.md`,
  `docs/explanation/memory-and-knowledge.md`, `docs/guides/manage-memory.md`.
- Automation: `docs/guides/automate-work.md`, `goal-mode.md`, `scheduler.md`,
  `watches.md`, `workflows.md`; no claim that every goal uses a deterministic verifier.
- Extension/operation: `docs/guides/plugin-registry.md`, `mcp.md`, `coding-agents.md`,
  `fleet.md`, `headless.md`, `observability.md`, `sandboxing.md`.
- Phone scope: `docs/guides/phone-access.md`, `pairing.md`,
  `apps/web/src/settings/PairRemoteDialog.tsx`; remaining scope in #4336.
- Listings: [huggingface.js #2372](https://github.com/huggingface/huggingface.js/pull/2372)
  and [Ollama #17738](https://github.com/ollama/ollama/pull/17738) both open on review date.
- Installer availability: `sites/marketing/src/lib/desktop-release.ts` resolves a release
  containing all four platform assets. Do not substitute the newest source release.
- Catalog ownership: `config/plugin-directory.yaml` → `scripts/plugin_directory.py`;
  roadmap ownership: `ROADMAP.md` → `scripts/roadmap.py`; release history: existing
  changelog/release pipeline. Do not hand-edit their derived data.

## Ordered execution ledger

| Task | Priority | Dependency | Status | Completion check |
| --- | --- | --- | --- | --- |
| Inventory and claim audit | P1 | Current source/docs and live site | Complete | Every route and shared surface above accounted for |
| Homepage and feature rewrite | P1 | Audit | Implemented; local checks passed | Readers can find first setup and each claim has a canonical owner |
| Use-case paths and navigation | P1 | Audience decision | Implemented; local checks passed | Desktop/mobile navigation and all task links work |
| Download/plugin corrections | P1 | Release and install contracts | Implemented; local checks passed | Four real installers remain; no incomplete source recipe |
| Roadmap and metadata | P2 | Current issue/PR state | Implemented; local checks passed | Generated roadmap current; canonical/social metadata valid |
| Product map and social card | P2 | New positioning | Complete; desktop/mobile visual review passed | Legible at intended dimensions; editable sources retained |
| Actual app captures | P1/P2 | Safe capture environment and owner | Planned | Four records in media plan satisfy their capture acceptance |
| Checks, review, merge, deployment | P1 | All implemented edits | In progress | Required gates pass, current-head review assessed, deployed revision verified |

## Media production

The authoritative briefs are in
[`sites/marketing/media/plan.json`](../../sites/marketing/media/plan.json), with
stable IDs, placement, priority, source watches, and capture acceptance. All four
product captures are **planned and unassigned**. The page contains no fake or visible
placeholder screenshots. The conceptual map is source-controlled Astro; the social
card has an editable HTML source and Playwright renderer. See the
[media workflow](../../sites/marketing/media/README.md) before producing or replacing assets.

## Verification

Local validation (2026-10-18): Astro check (0 errors, one existing navigator.platform
hint), Astro build (7 routes), VitePress build at `/docs/`, generated navigation/API/
plugin-directory/roadmap checks. Combined output: 13,748 local links/assets across
255 pages, no missing files/fragments or duplicate IDs. Playwright Chromium checked
all seven marketing routes in desktop light/dark and 390px mobile layouts: no
horizontal overflow or JavaScript errors; keyboard skip link, mobile navigation,
plugin search, canonical URLs, and no-JS platform choices passed. Social graphic
and desktop/mobile homepage captures visually inspected. Full Python gate: 14,069 passed / 44 skipped; all five checks passed. Integrated
main through `f44fde54`, preserving the concurrent roadmap refresh, then reran lint,
site builds and browser/link checks. Current-head PR review and deployment pending. Site validation does not prove a model-backed
product journey or native desktop playback. Actual captures remain separate production work.
