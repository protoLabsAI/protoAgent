- **Other plugins can drive a browser through core's hardened path: the `agent_browser.client` service.**
  A plugin that needed its own browser (ebay-plugin's price checks) shelled out to `agent-browser`
  with a bare `subprocess.run`. It had no output cap, no process-group kill, no per-session lock,
  no CLI resolution and no option guard, and every failure came back as a string. A stuck
  browser and a bot wall looked the same, so 14% of ebay price checks ran for 60 s or more
  (p95 126 s). `make = sdk.service("agent_browser.client")` now returns a sync, thread-safe
  `BrowserClient` on the plugin's own named session (instance-prefixed by default). It shares
  the tools' subprocess runner (`runner.py`, extracted from `tools.py` unchanged) and their
  per-session lock. It offers `open` / `wait` / `eval_json` / `get_text` / `snapshot` /
  `check_wall` / tab helpers, and each returns a typed `Result`: `ok`, `timeout`, `wall`,
  `cli_error`, `script_error`, `unavailable`, `bad_argument` or `too_large`. `open(url)` sends
  the plugin's launch options every time and refuses a URL-less `open`, which would relaunch
  Chrome onto a temp profile. Every wait carries an explicit timeout, so the CLI doesn't re-send
  a failing 30 s wait five times. A hung navigation is cut off after `nav_timeout_s` and
  recovered once. An `own_tab` label keeps navigation out of Chrome's Gemini side panel and the
  operator's tabs. Documented under "For plugin authors" in the browser-automation reference.
