- **Stream-drop failures name the real cause instead of guessing "rate limit" (#4959).**
  When a model stream drains after content has already streamed, the turn's error message
  now reflects why: a `StreamStallTimeout` (the model went silent past
  `model.request_timeout`) reports that the model stopped sending output and names the
  timeout window when the exception carries it; any other transport error reports that the
  connection to the provider dropped mid-reply. Neither mentions rate limiting — a real 429
  is still classified first and keeps its existing message. The `_fail_turn` warning log
  likewise drops the "possible rate limit" guess while keeping the exception type and text.
  The HTTP status (502) and error shape are unchanged.
