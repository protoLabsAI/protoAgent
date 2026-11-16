- **Subagent tiers accept a model fallback chain.** A subagent's pinned model
  (`subagents.<name>.model`) now accepts an ordered list as well as a single id (ADR 0133):
  the primary, then each fallback used only on a retryable provider error. A tier pinned to
  `[a, b]` whose primary `a` rate-limits (429) fails over to `b` instead of failing the whole
  delegation — the delegation runs on `a` and the chain's tail rides the same observable
  model-fallback middleware the lead agent uses. The pin still wins the existing precedence
  (pin → the turn's model override → `routing.aux_model` → the main model), a scalar pin stays
  a chain of one (unchanged), and a tier with no pin inherits the aux→main chain with the main
  chain's fallbacks still applying. The Settings provider-delete guard now counts a connection
  named by any chain entry of a subagent pin as in use, and releasing it preserves the tier's
  healthy fallbacks. Pins emptied by native-provider coherence filtering inherit the turn
  override, auxiliary model, or main model instead of attempting an unroutable alias.
