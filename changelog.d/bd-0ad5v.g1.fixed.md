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
  take back) — and `GET …/steer` now reports each restarted id in a `started` map
  (msg id → task id, `""` while the start is in flight), so the console's turn-end
  reconcile attaches those bubbles to the fresh turn and streams its reply in, instead
  of settling them as consumed by the turn that just ended and losing the answer.
  A steer (and a server-turn interjection) now also carries the tab's per-message
  `model` / `reasoning_effort` / `bypass_permissions`, exactly as a normal send stamps
  them, so the turn started for it runs under the tab's settings rather than the
  configured defaults; the deck TUI and the Zed ACP shim read `started_turn` and the
  `started` map too, following the fresh turn for its reply instead of reporting the
  message folded into the ended turn; and a restart that fails to start its turn takes
  the popped ids back OUT of the drain log when it requeues them, so the message can't
  read as both pending and already-read.
