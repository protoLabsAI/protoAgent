- **Plugins can read a form, and the agent's own browser, through the browser client.**
  `agent_browser.client` gains `form_read(scope="")`: the same in-page reader `browser_form_read`
  uses (react-select option probe included), returned as a list of field dicts (label, kind,
  name, id, required, value, options, options_more) with no content-boundary markers. And
  `make.for_agent_session(state)` builds a client on the session the agent's `browser_*` tools
  use for the calling chat (`shared` or `per_session`, resolved the way the tools resolve it),
  behind the same per-session guard, with the browser plugin's own launch flags and `close()`
  refused. careercoach's verify step can now read the real page instead of trusting a read-back
  the model passes in, which an eval caught being cut from 7.3 KB to 3.8 KB.
