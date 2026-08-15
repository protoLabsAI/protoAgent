- **Devkit lifecycle-tool fixes from the #2735 review findings (#2719).**
  `install_plugin` no longer claims "fetched only (activate=False)" when the
  enable-reload actually failed; `_live_apply` catches a raising reload and returns a
  clean failure like its sibling `_live_enable`; `disable_plugin` refuses builtins
  instead of reporting a false "✓ disabled" for a plugin the loader keeps live;
  `uninstall_plugin`'s bundle branch routes through the shared `ops.uninstall_bundle`
  instead of reimplementing it; and the bundle-preview cache is keyed by (url, ref)
  so two refs of one bundle stop sharing a preview within the TTL. The
  single-plugin update success branch (release-tag → newest semver → purge → live
  reload) is now tested.
