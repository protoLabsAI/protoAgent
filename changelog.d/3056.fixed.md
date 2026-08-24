- **Finished removing `daily_log`, and stopped a duplicate eval-case id from hiding
  regressions.** The tool left core a while back, but six places still described it as
  shipped: the evals guide (whose `--tasks current_time_intent,daily_log_intent` example
  named a case that no longer exists — and `--tasks` drops unknown ids *silently*, so it
  ran one case and reported green), the eval runner's own `--tasks` docstring example
  (`current_time,memory_ingest` — neither is a real case id), the `tools.disabled` example
  in `config/langgraph-config.example.yaml`, the `knowledge` package docstring, and ADR 0005's
  tool census. Worse, the eval case itself had been rewritten onto `memory_ingest` **without
  being renamed**, leaving two cases sharing the id `memory_ingest_intent` — and every report
  consumer keys by id (`compare.py` builds `{id: passed}`, `sweep.py` aggregates into
  `agg[id]`), so the second silently overwrote the first and a regression in either was
  invisible to both. The case is now `memory_note_intent`, a test asserts case ids are
  unique, the docs name real ids and warn that unknown ones are dropped without complaint,
  and ADR 0005's census is marked as the dated snapshot it is (it's the evidence for that
  ADR's count argument, so it's preserved rather than rewritten) with a pointer to the live
  [Starter tools](/reference/starter-tools) reference.
