- **Decisions record whether an answer was picked or typed.** An answered decision now
  stores `answer_kind`: "option" when a listed option was picked, "text" when the answer
  was typed. It is empty while the decision is pending, and on an expired, withdrawn or
  cancelled one. Every answer is recorded as "text" until the answer paths pass the real
  kind in a follow-up change. Refs #5668.
