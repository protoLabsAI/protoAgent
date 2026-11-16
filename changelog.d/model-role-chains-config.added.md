- **Model roles accept an ordered fallback chain.** Every model-role setting — the main
  `model.name`, `routing.aux_model`, `compaction.model`, `goal.eval_model`,
  `memory.select_model`, `knowledge.image_describe_model`, the soul drift judge, and the new
  `memory.harvest_model` / `memory.facts_model` / `memory.gist_model` — now accepts either a
  single model (today's shape) or an ordered list: the primary, then each fallback in turn
  (ADR 0133). `routing.fallback_models` still appends to the main chain. A blank role inherits
  its parent's whole chain (specific → aux → main; harvest → aux, facts → harvest, gist → aux),
  except a blank image-describe model, which stays off. Config-side only; the chain editors
  arrive in a later milestone.
