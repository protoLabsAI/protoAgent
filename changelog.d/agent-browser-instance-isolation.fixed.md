- **Every agent on a machine now drives its own browser.** The browser plugin left the
  `agent-browser` session name to the CLI, which calls it `default`. Every protoAgent
  instance on a machine is its own server, so every fleet agent drove one Chrome with one
  cookie jar. One agent saw another's page mid-review, three price checks from one agent
  returned another's dataset, parallel form fills crossed between agents, and every agent
  could use every other agent's signed-in sites. The plugin now names the session from the
  instance (`pa-<instance>-<hash>`) and passes it to every CLI call: the tools, the Browser
  panel's toolbar, and its live view. `session_scope: per_session` chat browsers are
  namespaced by instance too. An operator-set `AGENT_BROWSER_SESSION` still wins.
  **On upgrade**, an agent's open page in the old shared `default` browser isn't carried
  over. Its first browser call starts its own empty browser, so sign in again there, or point
  `profile` at a profile directory. The old `default` browser keeps running until it's closed
  (`agent-browser --session default close`) or the machine restarts. Two agents given the same
  `profile` directory can no longer both use it at once, because Chrome allows one browser
  per profile directory.
- **Idle per-chat browsers shut down.** With `session_scope: per_session`, each chat's
  browser now exits after `idle_timeout_min` (default 30) minutes without a browser
  command. Before, each one kept a Chrome running until the machine restarted. The agent's
  main browser has its own `main_idle_timeout_min`, off by default, because your signed-in
  tabs live there and watching the Browser panel doesn't count as activity. Both settings
  apply to browsers started after a change.
- **A browser profile held by another browser is now a clear error.** With a `profile`
  directory and `per_session`, the second chat's `browser_open` failed with Chrome's raw
  launch log, and the cause was cut off. The tools and the panel now say the profile
  directory is in use and how to fix it. The live tests also stop leaving browser daemons
  behind when a run is killed before teardown.
