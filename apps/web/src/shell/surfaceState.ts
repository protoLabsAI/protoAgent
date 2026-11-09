/** Opaque to the host: each view owns the meaning of its saved place. */
export type SurfaceState = {
  section?: string;
  itemId?: string;
  scrollTop?: number;
  filters?: Record<string, unknown>;
};

export type SurfaceLink = { viewId: string; state: SurfaceState };

/** Parse a relative surface link; malformed or empty segments are rejected. */
export function parseSurfaceLink(link: string): SurfaceLink {
  const parts = link.split("/").map(decodeURIComponent);
  if (parts.length < 2 || parts.length > 3 || parts.some((part) => !part.trim())) {
    throw new Error("Expected <view>/<section>[/<item-id>]");
  }
  const [viewId, section, itemId] = parts;
  return { viewId, state: { section, ...(itemId === undefined ? {} : { itemId }) } };
}

export function formatSurfaceLink(viewId: string, state: SurfaceState): string {
  const parts = [viewId, state.section ?? "", ...(state.itemId === undefined ? [] : [state.itemId])];
  if (parts.some((part) => !part.trim())) {
    throw new Error("Expected <view>/<section>[/<item-id>]");
  }
  return parts.map(encodeURIComponent).join("/");
}
