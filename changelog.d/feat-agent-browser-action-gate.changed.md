- **The browser asks before it submits — whatever the agent called the button.** Before a
  click, an Enter/Space press, or typed text with a newline, the Agent Browser plugin now works
  out what the action will really hit. A `@eN` ref is resolved through the CLI's box for it;
  a CSS selector or label is looked up in the page. A form submission, or a control named
  *Submit*, *Send*, *Place order*, *Pay*, *Delete* and the like, raises an approval card in
  plain words ("Submit the form on boards.greenhouse.io — button “Submit application”") before
  anything is sent. A job board's *Apply now*, filters, search, sign-in and *Next* are left
  alone. Previously `browser_click("@e42")` on the real Submit button went straight through,
  because only text in the tool arguments was ever checked. A subagent, workflow step, or
  scheduled, inbox or A2A turn can't answer a card, so it gets an immediate refusal instead of
  a hang. `confirm_actions` is now this gate's category list: `submit` (the default when
  unset), `eval`, `upload`, `download`, `all`, or `""`/`none` for off. It is no longer passed to
  the CLI as `--confirm-actions`, which waited for a confirmation nothing ever answered. Other
  plugins can layer rules on the gate through the `agent_browser.add_gate_policy` and
  `agent_browser.describe_target` services.
