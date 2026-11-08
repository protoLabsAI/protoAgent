- **A plugin can no longer shadow the core `/directives` chat command (#4848).** `/directives`
  is handled by the core chat dispatcher before plugin commands, so a plugin that registered its
  own `/directives` got a dead command that never ran yet still showed up — a second, broken entry
  in the command palette. `register_chat_command` now reserves `directives` alongside `goal` and
  `lifecycle`: the runtime warns and skips it, and the plugin testkit raises so a drifting plugin's
  own CI catches it.
