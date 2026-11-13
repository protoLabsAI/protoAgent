- **A message steered at a session with no live turn starts a turn instead of queueing forever (bd-0ad5v).**
  A console with a stale 'streaming' bubble sent the operator's next message as a
  mid-turn steer; with nothing running, the text sat `pending` with nothing left to
  drain it and the chat looked stuck. The steer POST now checks the session under the
  real turn lock: idle (no running turn, no HITL-parked interrupt) means the message
  runs as an ordinary operator turn over the same path as a normal send — same
  messageId, so a duplicate POST joins the first task — and the reply's
  `started_turn: <task_id>` tells the console to finalize its stale streaming state
  and attach to the new turn. A steer still queued when its turn ends `completed`/
  `failed` is likewise restarted as exactly one fresh turn (a park keeps holding its
  messages for the operator's answer; a Stop's queued messages stay the console's to
  take back).
