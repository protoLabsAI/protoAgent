- **Console: a server-started turn that finished while you were away no longer leaves a spinning empty bubble above its answer (bd-wk4ti).**
  A background delegation's result runs as a server-started turn; if you switched
  agents while it ran, the console's live preview for it could be stranded streaming —
  the bus events that settle it were missed — while durable hydration drew the turn's
  finished text as a second bubble beside it. Hydration now keeps exactly one bubble
  per task: when the server reports a turn terminal (and nothing live in the session),
  the reconcile redraws the turn's durable copy over the stranded placeholder — or
  finalizes the placeholder in place when the turn also has locally rendered bubbles
  to keep — and the session's locked "streaming" status is handed back. A preview that
  still holds the session's newest task id is also re-read on return, where the index
  previously read "in step" and never fetched the finished turn.
