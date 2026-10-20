// Saved data is untrusted too: bound collections and validate before rendering styles/URLs.
export type Swatch = { label: string; value: string; light?: string; copy?: string };
type Base = { title: string; created_at: string };
export type Palette = Base & { kind: "palette"; mode: "dark" | "light"; groups: { name: string; swatches: Swatch[] }[] };
export type Contrast = Base & { kind: "contrast"; foreground: string; background: string; ratio: number; aa_normal: boolean; aa_large: boolean; aaa_normal: boolean };
export type Story = { id: string; name: string; preview: string; docs: string };
export type ComponentPreview = Base & { kind: "component"; component_title: string; stories: Story[] };
export type Preview = Palette | Contrast | ComponentPreview;

function object(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null;
}
function label(v: unknown, max = 200): v is string {
  return typeof v === "string" && v.length > 0 && v.length <= max;
}
export function previewRef(props: Record<string, unknown>) {
  return typeof props.preview_id === "string" && /^[a-f0-9]{32}$/.test(props.preview_id)
    && label(props.title) ? { id: props.preview_id, title: props.title } : null;
}
export function color(v: unknown): v is string {
  if (typeof v !== "string" || v.length > 200) return false;
  // No var()/currentColor: a saved specimen must not change with the console's theme.
  return /^#(?:[\da-f]{3,4}|[\da-f]{6}|[\da-f]{8})$/i.test(v)
    || (/^(?:rgba?|hsla?|oklch|oklab|lab|lch|color)\([\d\s.,%+\-/a-z]+\)$/i.test(v)
      && typeof CSS !== "undefined" && CSS.supports("color", v));
}
function httpUrl(v: unknown): URL | null {
  if (!label(v, 4096)) return null;
  try {
    const u = new URL(v);
    return ["http:", "https:"].includes(u.protocol) && !u.username && !u.password ? u : null;
  } catch { return null; }
}

export function parsePreview(value: unknown): Preview | null {
  const p = object(value);
  if (!p || p.version !== 1 || !label(p.title) || !label(p.created_at, 64)) return null;
  const base = { title: p.title, created_at: p.created_at };
  if (p.kind === "palette") {
    if (!Array.isArray(p.groups) || !p.groups.length || p.groups.length > 16) return null;
    const groups: Palette["groups"] = [];
    let total = 0;
    for (const item of p.groups) {
      const g = object(item);
      if (!g || !label(g.name) || !Array.isArray(g.swatches) || !g.swatches.length) return null;
      total += g.swatches.length;
      if (total > 512) return null;
      const swatches: Swatch[] = [];
      for (const item of g.swatches) {
        const s = object(item);
        if (!s || !label(s.label) || !color(s.value) || (s.light !== undefined && !color(s.light))
          || (s.copy !== undefined && !label(s.copy, 240))) return null;
        swatches.push({ label: s.label, value: s.value, light: s.light as string | undefined, copy: s.copy as string | undefined });
      }
      groups.push({ name: g.name, swatches });
    }
    return { ...base, kind: "palette", mode: p.mode === "light" ? "light" : "dark", groups };
  }
  if (p.kind === "contrast") {
    if (!color(p.foreground) || !color(p.background) || typeof p.ratio !== "number"
      || !Number.isFinite(p.ratio) || p.ratio < 1 || p.ratio > 21
      || [p.aa_normal, p.aa_large, p.aaa_normal].some(v => typeof v !== "boolean")) return null;
    return { ...base, kind: "contrast", foreground: p.foreground, background: p.background,
      ratio: p.ratio, aa_normal: p.aa_normal as boolean, aa_large: p.aa_large as boolean, aaa_normal: p.aaa_normal as boolean };
  }
  if (p.kind === "component") {
    const root = httpUrl(p.storybook_url);
    if (!root || root.search || root.hash || !label(p.component_title)
      || !Array.isArray(p.stories) || !p.stories.length || p.stories.length > 100) return null;
    const path = root.pathname.replace(/\/$/, "");
    const stories: Story[] = [];
    for (const item of p.stories) {
      const s = object(item);
      if (!s || !label(s.id) || !label(s.name)) return null;
      const preview = httpUrl(s.preview), docs = httpUrl(s.docs);
      if (!preview || !docs || preview.origin !== root.origin || docs.origin !== root.origin
        || ![`${path}/iframe`, `${path}/iframe.html`].includes(preview.pathname)
        || preview.searchParams.get("id") !== s.id || docs.pathname !== `${path}/`
        || docs.searchParams.get("path") !== `/story/${s.id}`) return null;
      stories.push({ id: s.id, name: s.name, preview: preview.href, docs: docs.href });
    }
    return { ...base, kind: "component", component_title: p.component_title, stories };
  }
  return null;
}
