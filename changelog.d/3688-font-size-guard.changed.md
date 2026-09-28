- **Console literal-`font-size` guard (#3688).** Added `apps/web/src/app/fontSizeGuard.test.ts`,
  which sweeps every `apps/web/src` stylesheet (via a compile-time `?raw` glob, no `node:fs`)
  and fails on any literal px `font-size` declaration — including custom properties whose name
  ends in `font-size` (e.g. `--diffs-font-size: 12px`) — reporting sorted `src/<path>:<line>`.
  Comments are stripped first, so only live declarations count. The only exemptions are the
  root crash-fallback `app/app-crash.css` (whole file, #872) and the two `settings/devices.css`
  `.devices-code` pairing-code sites (`28px` / mobile `22px`), pinned by selector + value as the
  known DS-scale gap protoLabsAI/protoContent#534. This closes the type-scale migration: a
  hard-coded px `font-size` can no longer creep back into a shipped surface. Part of #3688.
