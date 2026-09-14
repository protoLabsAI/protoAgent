- **A room member the room stopped waiting on is no longer lost: its late answer is collected and posted (#3360).**
  When an `a2a` member's address gives up with the peer still working ("still running after Ns without
  observable progress"), the room keeps that task's id and polls it read-only (`GetTask`, backing off to 30s)
  until it settles. The answer is then posted to the chat as that member's own message, marked as arriving
  after its turn, and the lead gets a turn to take it in, the same way a background `delegate_to` reply
  arrives. A failure arrives as a failed message; a task that stops on a question hands the lead the question
  and its resume handle. Collection never sends the member anything, so it cannot open a duplicate task, and
  the member stays dropped from the room's remaining rounds. Rewinding or deleting the chat withdraws it, so
  an answer to erased history cannot reappear. The lead's own foreground `delegate_to` gets the same: it is
  told the answer will arrive on a later turn instead of re-delegating the work. Gives up after an hour, or
  after losing touch with the peer eight polls in a row, and says so. In-memory, so a restart ends it.
