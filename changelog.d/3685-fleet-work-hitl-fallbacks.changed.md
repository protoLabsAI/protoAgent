- **Console: drop the dead literal `var(--pl-X, …)` fallbacks in fleet-room / fleet-activity / work / hitl CSS (#3685).**
  `main.tsx` imports `@protolabsai/design` before any app CSS and `tokenNameGuard.test.ts`
  proves every `var(--pl-*)` resolves to an installed-DS token, so a literal fallback in
  `var(--pl-X, <literal>)` can never paint — it is dead, drifted code. The mono font stacks
  (`ui-monospace, "SF Mono", Menlo, monospace`) and the popover-shadow literals
  (`0 2px 8px rgba(…)` / `0 -10px 28px -14px rgba(…)`) are removed, leaving the bare
  `var(--pl-font-mono)` / `var(--pl-shadow-popover)` — rendering is unchanged. The two
  token-to-token fallbacks in `fleet-room.css` (the @-mention popover's raised-surface and
  strong-border sites) are left intact. A new source-level guard (`dsStaleFallbackDrop1b.test.ts`)
  pins the drop for the four files.
