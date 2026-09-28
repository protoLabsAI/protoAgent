- **The last legacy `--brand-*` accent aliases are deleted and the token-hygiene invariants go tree-wide (#3685).**
  `app/theme-base.css` was the final home of the retired `--brand-violet`/`--brand-violet-light`/
  `--brand-indigo`/`--brand-indigo-bright`/`--brand-pink` aliases (two of them still carrying a
  dark-only `var(--pl-color-accent, #hex)` fallback); with every consumer already re-pointed onto
  real `@protolabsai/design` tokens they are now removed, so `--brand-*` appears nowhere in the
  console. The `--success`/`--warning`/`--error`/`--danger`/`--info` status compat aliases are
  kept byte-identical. `app/tokenNameGuard.test.ts` gains two tree-wide sweeps that turn the
  earlier per-sheet strips into repo-wide guards: no `var(--pl-…, #hex)` fallback may ship outside
  `app/app-crash.css` and the `theme-base.css` status aliases, and the `--brand-` prefix may not
  appear anywhere but that guard (built by concat). Both report `src/<path>:<line>` and self-test
  their patterns.
