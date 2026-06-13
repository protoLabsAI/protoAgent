import type { SettingsField } from "../lib/types";

// #963 — is a settings field visible given the current form values? A field with no
// `depends_on` always shows; otherwise the named sibling's current value must satisfy
// the predicate: {equals} = strict equality · {in} = membership · neither = the sibling
// is truthy (the "enable X → show X's options" pattern). `valueOf` returns the live
// form value for a key (the dirty edit if any, else the saved value), so visibility is
// reactive to what's on the form right now, not just what was last saved.
//
// Pure + standalone (no React / no @protolabsai/ui) so the predicate is unit-testable.
export function fieldVisible(field: SettingsField, valueOf: (key: string) => unknown): boolean {
  const dep = field.depends_on;
  if (!dep?.key) return true;
  const cur = valueOf(dep.key);
  if ("equals" in dep) return cur === dep.equals;
  if (Array.isArray(dep.in)) return dep.in.includes(cur);
  return Boolean(cur);
}
