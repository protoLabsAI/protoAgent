- **An open chat follows a parked turn that is answered elsewhere, without a reload.**
  When a `request_decision` turn was answered from its card, from `POST /api/decisions/{id}/answer`,
  or by expiry, the server resumed the parked task over its own loopback call, so no browser
  stream carried the rest of the turn. The decision card updated, but the tool row stayed
  "waiting for you" and the operator's answer and the agent's reply appeared only after a
  reload. The console now re-attaches to the paused turn when the bus reports `turn.resumed`
  for it, so the continued turn streams in live, and a stale question form is cleared.
