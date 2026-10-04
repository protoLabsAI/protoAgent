- **`browser_select` sets native, react-select and phone-country dropdowns and verifies the result (#4032).**
  The `agent_browser` plugin gains one tool for CHOICE fields — a native `<select>`, a
  react-select-style combobox, and an intl-tel-input country picker — addressed by its visible
  LABEL or a CSS selector (a snapshot `@ref` is refused here: the widget is set in the page,
  where the CLI's ref can't be resolved). It matches `option_text`
  case-insensitively (an exact match, or a unique prefix; zero or several candidates is an
  error that lists the options), commits by CLICKING the matching option — never by pressing
  Enter — and CLEARS a combobox before typing rather than appending to it. The two failure
  modes on the Greenhouse form this targets are removed by construction: a type+Enter no
  longer commits the highlighted-but-wrong option (visa "Yes, Ireland Highly Skilled Worker
  Visa"), and typed text is no longer doubled ("YeYess"). After committing it reads the
  rendered value back and compares it to what was chosen; a disagreement is a hard `Error:`,
  so a wrong answer is never submitted silently. For a phone field, select the country first,
  then `browser_fill` the national number.
