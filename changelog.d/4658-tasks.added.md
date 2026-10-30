- **`scripts/evals_ab.py` takes `--tasks` and `--category` (#4658).** A quick learned-context
  A/B can now run just the cases in question instead of the whole suite twice (about 20 minutes).
  Both arms always run the same filter, so the comparison stays like-for-like.
