- **Agents that get their gateway key from the environment now learn their model's context window (#3502).**
  The window lookup authenticated with `model.api_key` only. A deployment that supplies the key through
  `OPENAI_API_KEY` instead (a fleet agent's stack env or secrets manager) sent the lookup with no key, and
  the gateway answered 401 "No api key passed in". The window then stayed unknown for the life of the
  process, so compaction, tool-result pruning and the context meter all ran on their fallbacks. The lookup
  now uses the same key as the gateway client.
