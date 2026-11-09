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
  a hang. An Enter/Space press whose focus is inside an iframe the gate can't read into is
  treated as a possible submit and asks too, rather than slipping an embedded form through
  unseen. `confirm_actions` is now this gate's category list: `submit` (the default when
  unset), `eval`, `upload`, `download`, `all`, or `""`/`none` for off. Only an explicit off-word
  turns it off — a value that names no known category (a legacy CLI verb like `click`, or a
  typo) fails closed to the `submit` default with a warning, so opting in can never silently
  leave the gate off. It is no longer passed to the CLI as `--confirm-actions`, which waited for
  a confirmation nothing ever answered. The bundled Fill Form workflow now stops at a filled,
  verified form and hands it back for the operator to submit — a workflow step is a subagent
  that can't answer an approval card, so it never clicks Submit itself. Other plugins can layer
  rules on the gate through the `agent_browser.add_gate_policy` and `agent_browser.describe_target`
  services.
