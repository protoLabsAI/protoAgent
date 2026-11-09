- **Edit a goal in place from the console (#4955).** Adds `PATCH /api/goals/{session_id}`, the
  route behind the Work view's goal Edit button. It takes a partial body and changes only the
  completion-contract fields (`outcome`, `constraints`, `boundaries`, `stop_when`) and the two
  per-goal budgets (`max_iterations`, `no_progress_limit`) — a single string is accepted for
  `constraints`/`boundaries` and coerced to a one-element list, and budgets reuse the goal-set
  bounds (a whole number in range). The condition, verifier, status and history are set once or
  owned by the drive loop, so a body naming any of them (or any other key) is a 400 and changes
  nothing. The edit is written through the goal store's own `set`, which re-fires `goal.changed`
  so every open console refreshes. An unknown session is a 404, goal mode switched off is a 503,
  and a `max_iterations` below the goal's current iteration is a 400.
