- **Tabbed AppShell columns use the DS `<Tabs attached>` fused-surface layout (#3688).** The
  plugin sub-tab strip now fuses to its panel card via `@protolabsai/ui` 0.63's `<Tabs attached>`
  (top-radiused `bg-raised` strip, border on every side but the bottom, next-sibling panel drops
  its own top edge) instead of the local `.pl-appshell__col > .pl-tabs` / `.pl-appshell__bottom`
  override, which is deleted. Rail and bottom-dock plugin views read as one card as before;
  embedded Configure mode stays detached.
