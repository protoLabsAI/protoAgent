- **Turning a flag on in Settings ▸ Developer now turns it on for the server too, and it stays on after a restart.**
  The toggle used to be saved in the browser only, so it changed what the console drew and
  nothing else. A flag that gates server behaviour, like Home, could be switched on in the
  panel while the server went on answering "not found"; the only way to really enable it
  was an environment variable, which on the desktop app is lost at the next reboot. The
  toggle is now stored on the agent (`PUT /api/flags/<id>`, one small file in the instance
  folder), the server reads it on every check, and Reset clears it. A flag pinned by its
  `PROTOAGENT_FLAG_<ID>` environment variable shows as "set by environment" with the toggle
  disabled, because the environment still wins. Each agent keeps its own toggles.
