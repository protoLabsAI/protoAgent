// Re-export barrel. The palette adapter now lives in `./palette/*` (see palette/index.ts
// for the map); this file stays so no importer had to move — App.tsx, Launcher.tsx,
// contextMenu/registrations.tsx and the seam tests all keep importing from here.
export * from "./palette";
