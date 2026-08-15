- **Bundle-lifecycle corrections from the #2732/#2736 review findings (#2718).**
  `uninstall_bundle` now buckets honestly — a member uninstalled individually earlier
  reports as already-gone instead of "kept (shared)"; the shared ownership scan is one
  helper instead of three copies. `POST /api/plugins/bundles/{id}/update` answers 404
  for an unknown bundle (matching the DELETE route). In `ops.update_bundle`, an
  explicitly passed ref is never silently replaced by the newest semver tag, retire
  failures accumulate instead of overwriting each other, and every graph-rebuilding
  apply now runs off the event loop. The bundles guide separates what the CLI update
  shares with the console path from what is console-only, and the lifecycle smoke
  reads the declared enable set from the lock row and exercises the shared-member
  keep leg.
