- **A2A delegation to a protoAgent peer is now bounded by lack of progress, not by one held-open request (#3360).**
  The `a2a` delegate now sends `SendMessage` with `returnImmediately`, so the peer hands the task back at once
  and the adapter polls it with `GetTask`. Before, a protoAgent peer held the request open for the whole turn:
  `poll_timeout_s` acted as a flat wall-clock cap (a long turn that was visibly making progress still failed at
  300s), and the failure left no task id behind, so nothing could come back for the peer's eventual answer.
  Now `poll_timeout_s` means what the rooms guide already said it meant — time since the last material change
  to the task — for every peer. An explicit `delegate_to(timeout=…)` still caps the whole wait. Polls ask for
  no task history, since the adapter never reads it. A peer that ignores `returnImmediately` answers inline,
  exactly as before.
