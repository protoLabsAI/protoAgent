- **The shipped Design System Engineer persona preset now reflects the archetype's current reach (#3719).**
  The preset (`config/soul-presets/design-system.md`) describes, in first person, the three
  capabilities it can perform today — adherence audits of a codebase or a live URL (a score plus
  findings split into system gaps vs. consumer fixes), breaking a rendered site into repeated UI
  patterns classified as covered / needs-a-variant / genuinely-missing, and generating a full
  dark + light brand theme against the live token contract with every foreground/background pair
  contrast-checked — and the two-board operating model: the persona owns the design-system repos
  and briefs the consuming app's project manager rather than editing the app, with cross-repo work
  not called done until it is verified merged **and** published. All prior principles and the
  Personality section are preserved; it stays pure persona (ADR 0079).
