- **Subagent tiers accept a model fallback chain.** A subagent's pinned model
  (`subagents.<name>.model`) now accepts an ordered list as well as a single id (ADR 0133):
  the primary, then each fallback used only on a retryable provider error. A tier pinned to
  `[a, b]` whose primary `a` rate-limits (429) fails over to `b` instead of failing the whole
  delegation, resolved through the same role-chain machinery the model roles use. The pin
  still wins the existing precedence (pin → the turn's model override → `routing.aux_model` →
  the main model), a scalar pin stays a chain of one (unchanged), and a tier with no pin
  inherits the aux→main chain with the main chain's fallbacks still applying.
