- **Decisions reach your phone again while the chat is open in a console.** v0.258.0
  dropped the phone push for a decision whose chat was open in any console, so a desktop
  app that keeps a chat open stopped every decision push. That change (#5663) is reverted:
  a pending decision always pushes. A decision that is already answered, withdrawn,
  cancelled or expired still does not. Refs #5648.
