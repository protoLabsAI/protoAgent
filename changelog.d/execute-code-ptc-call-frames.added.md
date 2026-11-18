- **A script's bridged tool calls now nest under their execute_code card.** The turn
  stream turns the execute_code plugin's `ptc_call` custom event into an ordinary tool
  frame carrying `parentId`, so the console files each call the running script makes under
  its execute_code card (name-prefixed `ptc:` to tell it from a model's or subagent's
  call) instead of floating it at the turn's top level.
