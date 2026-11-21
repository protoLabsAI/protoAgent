- **A self-improvement review is no longer refused when the finished goal's text names a tool.**
  The check that stops a subagent prompt asking for a tool the subagent lacks now skips
  fenced code blocks, so quoted JSON, logs and diffs do not count as asking for a tool.
  Tool names outside a fence are still caught as before. Fixes #5677.
