- **Drop dead literal `var(--pl-*)` fallbacks in goals/memory CSS (#3685).** `main.tsx` loads
  `@protolabsai/design` before any app CSS and `tokenNameGuard.test.ts` proves every `var(--pl-*)`
  resolves, so a literal fallback can never paint — it is drifted, dark-only dead code. Rewrote the
  two audited sites to read the bare token: `goals.css` `.goal-row:hover` background
  (`var(--pl-color-bg-hover, rgba(127,127,127,0.06))` → `var(--pl-color-bg-hover)`) and `memory.css`
  `.memory-detail-snippet` color (`var(--pl-color-fg, inherit)` → `var(--pl-color-fg)`). Added
  `apps/web/src/app/dsStaleFallbackGoalsMemory.test.ts`, a compile-time `?raw` guard (no `node:fs`)
  that fails if a literal `var(--pl-*, …)` fallback creeps back into either sheet while still allowing
  nested token fallbacks (`var(--pl-x, var(--pl-y))`). Part of the DS-adoption audit (rule
  stale-fallback).
