- **Workflows surface CSS drops its stale dark-only hex fallbacks (#3685).**
  Every `var(--pl-…, #hex)` in `apps/web/src/workflows/workflows.css` — status
  dots, badges, borders and accent text, including the ones nested in
  `color-mix()` — is now a bare `var(--pl-…)`. The design package's tokens are
  always loaded before any app CSS, so those `#hex` fallbacks could never paint
  anything but a wrong, dark-only colour; removing them lets the workflow
  status/accent colours track the active theme (they resolve to the light token
  values under `data-theme="light"`). No token names change and no new hex
  literals are added; token-to-token `var()` fallbacks are left untouched.
