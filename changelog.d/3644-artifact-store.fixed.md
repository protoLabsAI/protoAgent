- **Artifact plugin resolves its store through the instance plugin store, honouring the box root (#3644).**
  `_store_path()` derived the store's directory straight from the home dir
  (`~/.protoagent/artifact[/<PROTOAGENT_INSTANCE>]`), ignoring `PROTOAGENT_BOX_ROOT` /
  `PROTOAGENT_HOME` (ADR 0004 / 0065): a box-scoped server wrote its artifacts into the
  real home, and a default install with no instance stored them one level ABOVE the
  instance root. It now resolves through `sdk.plugin_store(plugin_id="artifact")` — the
  same seam the friction and notes plugins use — so the dev sandbox and every fleet member
  get their own copy. A pre-scoping store under `~/.protoagent/artifact[/<inst>]` is
  migrated on first access — both `history.json` AND its `blobs/` move together, so
  file-artifact downloads keep resolving — while sibling instance subdirectories under a
  bare legacy dir are left where they are. `ARTIFACT_DIR` is unchanged (still an env-only
  override, still `/<PROTOAGENT_INSTANCE>`-scoped), and a path-resolution failure falls
  back to the legacy path so a store access never fails over where its file lives.
