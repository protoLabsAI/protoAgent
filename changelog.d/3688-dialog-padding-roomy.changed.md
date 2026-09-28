- **Console: playbook & plugin content dialogs opt into DS 0.63 `<Dialog padding="roomy">` (#3688).**
  The skill author/edit dialog (`PlaybooksSurface`), the git-URL install dialog
  (`InstallPluginDialog`), the per-plugin settings dialog (`PluginSettingsDialog`) and the
  install-time Python-deps prompt (`depsInstall`) now pass `padding="roomy"`, which `@protolabsai/ui`
  0.63.0 renders as `.pl-dialog__body--roomy` (24px). While the app-wide
  `.pl-dialog__body { padding: 24px }` rule still exists these dialogs render identically; the opt-in
  is what preserves their 24px body padding once a later card deletes that global rule (the DS default
  is 16px). No CSS changes. Part of #3688 (DS 0.63 card 3, Dialog body padding).
