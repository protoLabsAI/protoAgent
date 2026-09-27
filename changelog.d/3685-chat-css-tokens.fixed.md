- **Chat, tool-call and prompt-viewer accents now follow the theme instead of a frozen dark fallback (#3685).**
  `chat-component.css`, `tool-calls.css` and `promptviewer.css` pinned a `var(--pl-…, #hex)`
  fallback on every accent/surface/focus site and leaned on the retired `--brand-violet*`
  aliases. Since the design package's tokens are always loaded, those hex fallbacks could only
  paint a wrong, dark-only colour, and the aliases bypassed the semantic tokens. All fallbacks
  are dropped for the bare `var(--pl-…)`; tool-call accent TEXT (chips, links, calc result,
  wait icon, editor links) now reads `--pl-color-accent-fg` so it stays readable on the card
  body under `data-theme="light"`, and the prompt-viewer budget bars/borders read
  `--pl-color-accent`.
