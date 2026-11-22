- **Steering a conversation another agent started now stays between you and your agent.**
  When you steered a thread a peer had started (the ones under From others), your words were
  queued into the peer's running turn or saved into task history the peer could read back.
  The server now takes the private path for every steer into such a thread, whether or not
  the request asks for it: the text is kept in the operator-only partition, runs as its own
  turn, and is absent from the peer's stream, its subscription, GetTask and the stored
  history. A steer that cannot be private is refused instead of being sent publicly: 403
  when private steering is off or the caller is not a signed-in operator, 409 when the
  thread is waiting on the peer's own question. Steering your own chats is unchanged. Known
  limit: the agent's own context still holds the text. Refs #5400, Refs #5401.
