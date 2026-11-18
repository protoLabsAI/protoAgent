- **`search_files` can run several queries in one call.** Pass `queries=["…", "…"]` (up to 8)
  instead of `query` and each one runs through the same search path as a single query —
  ripgrep fast path or Python walk, honouring `path`, `regex`, `context_lines` and
  `include_generated` — and renders under its own `## <query>` heading, in input order, with
  its own per-query caps and no-match / unreadable-path notes. The combined output shares one
  400-line budget: past it the result is cut at a line boundary and ends with
  `… (output cap reached after query K of N; narrow the queries or split the call)`, so the
  queries that were not shown are named rather than dropped silently. A coding agent no longer
  pays one model round trip per pattern, or loops over patterns in the shell. The single
  `query` form returns exactly what it did before; passing both forms, neither, more than 8
  queries, or an empty string inside `queries` is a clear error. Refs #5098.
