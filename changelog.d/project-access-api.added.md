- **The operator API can switch a managed project between read-only and read-write.**
  `PATCH /api/projects/{name}` with `{"write": true|false}` changes the access mode of a registered project in
  one call, persisted and applied live, with no hand-edit of the agent's config. It goes through the same shared
  helper as the onboarding tools (`tools.onboard_tools.set_project_write`), so it rewrites only the `write` key —
  on the `projects:` entry and on an explicit `filesystem.projects` entry that already mirrors it — and never
  materializes the registry into explicit fence entries. A fence-only entry is found by name too. The caller is
  the operator, so there is no approval card; setting the mode a project already has writes nothing. Unknown name
  is a 404, a missing or non-boolean `write` a 400. The console client gains `api.setProjectWrite(name, write)`
  for the upcoming Settings ▸ Projects toggle. The change is merged under the config write lock, so two
  overlapping switches — or a switch overlapping an agent registering a project, whose own write now merges the
  same way — cannot undo each other. A registry entry's current mode is read the way the fence enforces it (a
  hand-edited `write: "false"` is read-only, a missing `write` is read-write), so a switch is never skipped as
  "already set" when the enforced mode differs. Refs protoLabsAI/projectBoard-plugin#783.
