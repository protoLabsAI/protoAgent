- **Starter prompts are now configurable per agent (#4570).** The empty-chat "Try one of these"
  chips are no longer hard-coded. A new `chat:` config section — `chat.show_starter_prompts`
  (a "Show starter prompts" toggle in Settings) and `chat.starter_prompts` (a list of
  `{label, prompt, note?}`) — lets an operator hide the chips or replace them with prompts that
  fit their agent. The resolved list is served in the console runtime status: `[]` when the toggle
  is off, the configured list when set, otherwise the built-in defaults. Malformed entries are
  dropped without breaking config load, and the list is capped at eight.
