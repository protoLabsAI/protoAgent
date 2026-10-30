- **Nightly regression entries say which check failed (#4656).** Each gate-regression friction
  entry now carries a `failed:` line with the case's failing check, for example
  `missing tools: ['memory_list']`. `eval_regression_triage` can only read the entry and the
  trace, not the report file, so in its first live run it could not tell a tool-choice slip
  from a real defect.
