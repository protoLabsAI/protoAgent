- **`execute_code` reports each tool call its script makes as a live `ptc_call` event.**
  A script run by `execute_code` calls tools through a bridge, and until now none of
  those calls reached the chat stream: the console showed one `execute_code` card and
  nothing inside it until the script finished. The plugin now emits a LangChain custom
  event named `ptc_call` when a bridged call starts and again when it ends, on both the
  one-shot path and the session-kernel path. Each event carries a unique call id, the
  tool name and the `parent_call_id` of the owning `execute_code` call; the end event
  adds the output (first 2000 characters, with the full length in `output_chars`),
  an `error` flag and `duration_ms`. A call blocked by policy emits both events, with
  `error: true` and the "Blocked by policy:" text. A run emits events for its first 200
  bridged calls only; later calls still run and are still audited. Emitting never
  changes a tool result. This is the first of three parts: the events are not yet
  turned into chat frames or drawn in the console.
