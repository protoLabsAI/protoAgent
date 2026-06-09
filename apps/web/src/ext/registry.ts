import type { ReactNode } from "react";

// Build-time fork seam (ADR 0038 D3). A fork drops a `src/ext/<name>.tsx` that calls
// registerSurface() to add a console rail surface — WITHOUT editing core App.tsx, so a
// `git pull upstream` stays conflict-free. Fork surfaces are compiled into the fork's build
// (trusted, in-process) — distinct from plugins (runtime, sandboxed iframes, ADR 0026/0038).
export type ExtSurface = {
  id: string;
  label: string;
  icon: ReactNode;
  placement?: "left" | "right"; // which rail (default: left)
  // Gate the surface on a plugin being enabled (its id in runtime.plugins). The rail item
  // is hidden + the surface unreachable unless that plugin is on. Used by first-party
  // optional surfaces extracted to plugins (e.g. workflows → plugins/workflows).
  requiresPlugin?: string;
  render: () => ReactNode;
};

const _surfaces: ExtSurface[] = [];

export function registerSurface(surface: ExtSurface): void {
  if (_surfaces.some((s) => s.id === surface.id)) return; // first wins (HMR-safe)
  _surfaces.push(surface);
}

export function registeredSurfaces(): ExtSurface[] {
  return _surfaces;
}
