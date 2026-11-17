- **Private conversations: honest exits on error, cancel and timeout.** A privately
  steered turn that ends abnormally now leaves the peer a content-free terminal
  ("working", then "failed" or "cancelled") instead of hanging: its tool arguments, tool
  output, progress, error strings and partial answer stay in the private partition, the
  unsent private buffers are discarded, and the delivery record is closed so a
  sealed-but-undelivered result is never shipped once the turn gives up. The mechanism
  stays dormant — no console surface and no switch — and only the proof tests exercise it.
  Refs #5400 (ADR 0131, P1e part 2 of 2).
