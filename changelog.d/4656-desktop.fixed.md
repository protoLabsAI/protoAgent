- **Nightly gate-regression triage works for desktop app members (#4656).** With
  `--friction-log` naming another agent's ledger, the nightly no longer tries to queue triage
  on its own scheduler, which belongs to a different agent; that member runs
  `eval_regression_triage` on its own scheduled job instead. The nightly now re-runs each
  regressed case once on a fresh agent and writes a `rerun:` line (PASS, FAIL or unavailable)
  into the friction entry. Triage uses that line when it can't re-run cases itself, which is
  the case in the packaged desktop runtime: `eval_rerun_case` now refuses there instead of
  trying to start a server from the frozen binary. `scripts/evals_ab.py --ab-learned-from-dir`
  takes a desktop member's directory as the A/B source.
