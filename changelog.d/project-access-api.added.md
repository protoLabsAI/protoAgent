- **The operator API can switch a managed project between read-only and read-write.**
  `PATCH /api/projects/{name}` with `{"write": true|false}` changes the access mode of a registered project in
  one call, persisted and applied live, with no hand-edit of the agent's config. It goes through the same shared
  helper as the onboarding tools (`tools.onboard_tools.set_project_write`), so it rewrites only the `write` key —
  on the `projects:` entry and on an explicit `filesystem.projects` entry that already mirrors it — and never
  materializes the registry into explicit fence entries. A fence-only entry is found by name too. The caller is
  the operator, so there is no approval card; setting the mode a project already has writes nothing. Unknown name
  is a 404, a missing or non-boolean `write` a 400. The console client gains `api.setProjectWrite(name, write)`
  for the upcoming Settings ▸ Projects toggle. Refs protoLabsAI/projectBoard-plugin#783.
