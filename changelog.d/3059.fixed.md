- **`python -m evals.runner --tasks` now says when an id doesn't exist.** It filtered
  with `c["id"] in wanted` and said nothing about the ids that matched nothing, so the
  dangerous shape was a *partially* unknown request: the run proceeded on whatever
  matched, exited 0, and printed a green board while covering less than was asked for. A
  typo and a retired case looked identical to a pass — which is how the evals guide came
  to document `--tasks current_time_intent,daily_log_intent` for months after that case
  was removed, running one case and reporting success. Unknown ids are now named on
  stderr (`warning: no such case id(s): … — continuing with the N that matched`), the
  selection moved into a testable `select_by_ids()` that also tolerates whitespace
  (`--tasks "a, b"` works), and a test gates the guide's own `--tasks` examples against
  `tasks.json` so a doc example can't rot into a lie again. A fully-unknown request
  already exited 2 and still does.
