- **Answered decisions now record the real answer kind.** Picking a listed option through
  the answer route records `answer_kind` "option"; a typed answer, including one typed into
  the chat composer, records "text". Restart-recovery and spend-cap decisions record the
  kind the same way. Refs #5668.
