import { describe, expect, it } from "vitest";

// protoContent#551 — the DS audit rule `hand-rolled-control` flags three raw <button>
// elements in the console that are deliberate COMPOSITE controls: the whole-row drawer items
// (icon + label) and the composer's model-menu trigger. The DS owner (designSystem) ruled
// they stay raw buttons, marked with the audit's line-level exception comment; the audit
// heuristic is NOT being widened. This test pins the exact, load-bearing form of that
// exception so a well-meaning reformat can't quietly break it and reopen the finding:
//   - it is a JSX-tag-internal block comment `/* … */` (a `{/* … */}` is a TSX syntax error
//     at the surfaces `.map(...)` return and the `trigger={...}` prop), so it must sit INSIDE
//     the opening tag, on the SAME line as the `<button`;
//   - the matcher (design-system-plugin audit.py:231) reads the lowercase words right after
//     `ds-audit-ignore` — separated by a single space, no colon — as the suppressed rule ids,
//     and the em dash ends that list, so `hand-rolled-control` must follow it directly;
//   - it is the same-line form, NOT `ds-audit-ignore-next-line` (the line above :74 / :97 is
//     not a place a comment can go).
//
// Source-level (Vite ?raw), same rationale as the sibling ds*.test.ts guards: the assertion
// is about the source TEXT the linter reads, not runtime behaviour, and the DS ships from a
// private registry. The `<button` token and the em dash are built by concatenation so this
// guard file never contains a bare raw-button tag and can't flag itself under the audit.

const TSX_SOURCES = import.meta.glob("../**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): `../<dir>/<name>`. Match by
// path suffix so a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(TSX_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`source not found in ?raw glob: ${suffix}`);
  return hit[1];
}

// The opening-tag token and em dash assembled by concat, so this file carries no bare
// `<button` element nor the exact sentinel as a self-standing tag.
const BTN = "<" + "button";
const EMDASH = "—";
// The exact exception comment designSystem pinned, character for character.
const EXCEPTION = `/* ds-audit-ignore hand-rolled-control ${EMDASH} composite row/trigger, protoContent#551 */`;

// A faithful copy of the audit matcher (design-system-plugin audit.py:231): capture the
// lowercase rule-id list that follows `ds-audit-ignore`.
const MATCHER = /ds-audit-ignore(-next-line)?(?:[:\s]+([a-z][a-z,\s-]*))?/;

// Each sanctioned composite <button> and the 1-based source line it sits on. The comment is
// inline, so adding it did not move these lines — the acceptance criteria name exactly these
// positions, and the audit reports its findings there.
const TARGETS = [
  { suffix: "/AppDrawer.tsx", line: 74, note: "surface-row button" },
  { suffix: "/AppDrawer.tsx", line: 91, note: "Settings-row button" },
  { suffix: "/ComposerModelSelect.tsx", line: 97, note: "model-menu trigger" },
];

describe(`sanctioned composite ${BTN}> DS-audit exceptions (protoContent#551)`, () => {
  for (const { suffix, line, note } of TARGETS) {
    describe(`${suffix}:${line} (${note})`, () => {
      const lineText = () => source(suffix).split("\n")[line - 1] ?? "";

      it("opens the button on the pinned line (line numbers must not move)", () => {
        expect(lineText().trimStart().startsWith(BTN + " ")).toBe(true);
      });

      it("carries the exact exception comment immediately after the tag, on the same line", () => {
        expect(lineText()).toContain(`${BTN} ${EXCEPTION}`);
      });

      it("is an in-tag block comment, not a {/* … */} expression or the -next-line form", () => {
        const l = lineText();
        expect(l.includes("{/*")).toBe(false);
        expect(l.includes("ds-audit-ignore-next-line")).toBe(false);
      });

      it("the audit matcher suppresses `hand-rolled-control` for this line", () => {
        const m = MATCHER.exec(lineText());
        expect(m, "no ds-audit-ignore matched on the button line").not.toBeNull();
        expect(m![1], "must be the same-line form, not -next-line").toBeUndefined();
        const rules = (m![2] ?? "").split(",").map((r) => r.trim()).filter(Boolean);
        expect(rules).toContain("hand-rolled-control");
      });
    });
  }

  it("sweeps real source — a stubbed (empty) ?raw import would blind the guard", () => {
    for (const { suffix } of TARGETS) {
      expect(source(suffix).length, `${suffix} imported empty`).toBeGreaterThan(0);
    }
  });

  it("the exception did not disturb the buttons' surrounding markup (attributes intact)", () => {
    const drawer = source("/AppDrawer.tsx");
    // Surfaces row: comment ends the `<button` line, the attributes stay on the lines below.
    expect(drawer).toContain(`${BTN} ${EXCEPTION}\n`);
    expect(drawer).toContain('className={`app-drawer-item${s.id === activeSurface ? " on" : ""}`}');
    // Settings row: single-line opening tag, comment then the original attributes verbatim.
    expect(drawer).toContain(
      `${BTN} ${EXCEPTION} type="button" className="app-drawer-item" onClick={act(() => onOpenGlobal())}>`,
    );
    const composer = source("/ComposerModelSelect.tsx");
    expect(composer).toContain(
      `${BTN} ${EXCEPTION} type="button" className="composer-model-select" aria-label="Model for this chat">`,
    );
  });
});
