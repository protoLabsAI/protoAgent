- **`write=true` on an already-registered read-only project now asks the operator instead of being ignored.**
  `onboard_project` and `register_local_project` used to answer "already registered … (read-only) — nothing
  changed" and drop the `write` argument, so an agent that needed a coder to build in a read-only project had no
  way to ask, and the operator had to hand-edit config. They now park on an approval card ("Make this project
  read-write?" — Allow read-write / Keep read-only) composed from the folder's realpath and bound to it; only that
  allow answer writes, and a denial, dismissal, plain or automatic approve, stale answer or a caller that can't
  approve changes nothing. `/bypass` and "allow for this session" do not skip it. `write=false` on a read-write
  project lowers it at once (narrowing needs no approval). The change goes through one shared helper
  (`tools.onboard_tools.set_project_write`) that rewrites only the `write` key on the `projects:` entry and its
  mirrored explicit `filesystem.projects` entry. The coding-delegate read-only refusal now names this path.
  Refs protoLabsAI/projectBoard-plugin#783.
