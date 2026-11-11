- **Answering the same decision from two places at once no longer tells both "Sent".** When the
  console and another surface (or two API calls) answered a pending decision at the same moment,
  both got a success and the losing one left a stray failed task and an "Operator chose…" bubble
  in the chat. Now the second answer waits for the first to finish, sends nothing of its own, and
  gets "Already answered" with the answer that won. If the first fails to deliver, the second
  goes through.
