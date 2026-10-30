- **The learned-context A/B report names the model it measured (#4658).** Arm reports were
  labelled `ab-A`/`ab-B`, so the comparison headline read "Model: `ab-B`" and `evals.report`
  trended the arms under those names. They are now labelled `<model> @ ab-A` / `<model> @ ab-B`,
  matching `evals.sweep`'s convention.
