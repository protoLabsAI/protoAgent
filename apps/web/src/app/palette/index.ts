// The command-palette adapter (ADR 0057), split out of the old 358-line
// `app/usePaletteRegistry.ts` when the host took ownership of the root view.
//
//   nav.ts       the ONE navigation chokepoint (openView / applyNavIntent / the launcher sink)
//   rank.ts      inclusion (a verbatim port of the DS matcher) + ordering
//   recents.ts   the namespaced frecency store, and its migration off the fleet key
//   rootView.tsx the host-owned root view registered as DS view id "commands"
//   registry.ts  the adapter itself: what core contributes, and in what order
//
// plus one contributor that registry.ts calls rather than the barrel re-exporting it, since
// nothing outside the package needs its symbols:
//
//   keybindingCommands.ts  the triaged keyboard actions as rows (ADR 0063 x ADR 0061)
//
// `app/usePaletteRegistry.ts` stays as a re-export barrel over this module, so every
// existing importer (App.tsx, Launcher.tsx, contextMenu/registrations.tsx, the seam tests)
// is untouched by the move.
export * from "./nav";
export * from "./rank";
export * from "./recents";
export * from "./registry";
export * from "./rootView";
