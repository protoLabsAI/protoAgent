- **Chat messages can address a delegate directly with `@name` (#3037, S1).** A chat
  message that opens with `@<delegate>` is now routed straight to that delegate over
  `DelegateRegistry.dispatch()` — the chat analogue of the `delegate_to` tool — and the
  LLM turn is skipped entirely. The check runs as STEP 0 in both the streaming and
  non-streaming turn drivers, before goal control and the slash-command cascade, so an
  `@`-mention is never swallowed by an active goal. `@Proto build it` matches the
  registered `proto` case-insensitively and sends only `build it`; a bare `@proto` returns
  a usage hint; a leading `@name` that resolves to no delegate answers with the roster
  (`Unknown or unreachable delegate: @name. Available: …`) rather than running the raw text
  as a prompt. The `@` must be the first non-whitespace character (no mid-message mentions
  in v1), so `hello @proto` stays a normal turn. The delegates plugin publishes the live
  roster on `STATE.delegate_registry` at register() time (cleared when the roster is empty),
  so with the plugin absent or no delegates configured every `@` falls through to ordinary
  chat. Both the `@name rest` message and the delegate's reply ride the same terminal path
  every slash-command short-circuit uses, so both land in session history.
