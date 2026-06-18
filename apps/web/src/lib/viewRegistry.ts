// ADR 0056 — a minimal unified-View façade. One addressable `View` resolved by id
// over the existing surface sources (core literals, plugin views, src/ext), with no
// migration of those sources. ADR 0056 calls for `viewFor(id) → View`; the command
// palette (ADR 0057) is its first consumer, so the minimal version lands here.
import type { ReactNode } from "react";

export type ViewKind = "surface" | "session" | "plugin" | "ext";

/** The addressable, placeable handle. Content stays in its own registry, resolved by id. */
export type View = { id: string; kind: ViewKind; title: string; icon?: ReactNode };

export type ViewSources = {
  /** Core surfaces (CORE_SURFACES) — `{ id, label, icon }`. */
  core: { id: string; label: string; icon?: ReactNode }[];
  /** Plugin views (ADR 0026) — keyed `plugin:<id>:<view>`. */
  plugins: { key: string; label: string; icon?: ReactNode }[];
  /** Fork/ext surfaces (`src/ext`), already gated by `requiresPlugin`. */
  ext: { id: string; label: string; icon?: ReactNode }[];
};

/** Unify the three sources into one `View[]` + a `viewFor(id)` resolver. First write
 *  of an id wins, so a core surface beats an ext one that claims the same id (e.g. the
 *  chat slot). */
export function buildViews(sources: ViewSources): {
  views: View[];
  viewFor: (id: string) => View | undefined;
} {
  const map = new Map<string, View>();
  const add = (v: View) => {
    if (!map.has(v.id)) map.set(v.id, v);
  };
  sources.core.forEach((s) => add({ id: s.id, kind: "surface", title: s.label, icon: s.icon }));
  sources.ext.forEach((s) => add({ id: s.id, kind: "ext", title: s.label, icon: s.icon }));
  sources.plugins.forEach((v) => add({ id: v.key, kind: "plugin", title: v.label, icon: v.icon }));
  return { views: [...map.values()], viewFor: (id) => map.get(id) };
}
