- **The fleet roster now stores operator switcher groups and per-agent pins (#4727).**
  Groundwork for the approved fleet-switcher redesign (docs/design/fleet-switcher-groups.md):
  the hub persists an ordered list of named groups and, keyed by each agent's immutable id, its
  group assignment and pin (with the time it was pinned) alongside the roster order in
  `roster.json`, so they are shared by every browser and survive a restart. `GET /api/fleet`
  now returns a top-level `groups` array plus optional `group`, `pinned`, `pinned_at` and (for
  remotes) `machine` on each agent — all optional, so an older hub still returns today's flat
  shape. New hub-only writes create, rename, reorder and delete a group, assign an agent to a
  group, and pin or unpin — gated like the existing Fleet settings order write. Remote agents
  are grouped automatically by their machine and can't be assigned to a group, but can be
  pinned; deleting a group ungroups its agents. No console UI yet — that lands in a later card.
