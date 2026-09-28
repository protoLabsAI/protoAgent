import { describe, expect, it } from "vitest";

// #3688 DS 0.63 card 3f — every CONTENT dialog opts into `<Dialog padding="roomy">`.
// theme.css still sets `.pl-dialog__body { padding: --pl-space-6 }` app-wide, so while that
// global rule lives this is a no-op render-wise; but a later card DELETES that rule, at which
// point any dialog that didn't opt in silently drops to the DS default 16px. This guard pins
// the opt-in on the four content-dialog files so a regression (or a newly added <Dialog>)
// can't ship un-roomy.
//
// Source-level (Vite `?raw`) rather than a rendered assertion — same pattern as
// settings-type-scale-tokens.test.ts / app/statusTokenGuard.test.ts. The DS Dialog only
// applies `.pl-dialog__body--roomy` when it receives padding="roomy" (verified against the
// @protolabsai/ui 0.63 component), so asserting the prop is present is asserting the class.
import pathPickerSrc from "./PathPicker.tsx?raw";
import providersSrc from "./ProvidersPanel.tsx?raw";
import quickSettingSrc from "./QuickSetting.tsx?raw";
import archetypePreviewSrc from "../setup/ArchetypePreviewDialog.tsx?raw";

// Each content-dialog file and how many `<Dialog>` openings it is expected to hold. The count
// is load-bearing: a file that grows a new dialog without padding="roomy" trips it.
const FILES: Array<[name: string, src: string, dialogs: number]> = [
  ["settings/PathPicker.tsx", pathPickerSrc, 1],
  ["settings/ProvidersPanel.tsx", providersSrc, 2],
  ["settings/QuickSetting.tsx", quickSettingSrc, 1],
  ["setup/ArchetypePreviewDialog.tsx", archetypePreviewSrc, 1],
];

// Match `<Dialog` as a component open (followed by whitespace or `>`), NOT `<ConfirmDialog`
// (that is a distinct DS component the card does not touch) nor a hypothetical `<DialogFoo`.
const DIALOG_OPEN = /<Dialog(?=[\s>])/g;

/**
 * Return the opening `<Dialog …>` tag as a string, starting at `start` (the index of `<`).
 * Scans to the first `>` at JSX-expression depth 0 — "depth" tracking `{…}` — so a
 * `footer={<>…</>}` prop's inner `>`s and a `title={`…${x}`}` template's braces can't be
 * mistaken for the tag's own close.
 */
function openingTag(src: string, start: number): string {
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    else if (ch === ">" && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error("unterminated <Dialog tag");
}

function dialogOpenings(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(DIALOG_OPEN)) out.push(openingTag(src, m.index));
  return out;
}

describe("content dialogs opt into DS padding=\"roomy\" (#3688 card 3f)", () => {
  it("imports the real source text, not empty stubs", () => {
    // If `?raw` ever stops returning file text these guards would pass vacuously — fail loud.
    for (const [name, src] of FILES) {
      expect(src.length, `${name} imported empty — check the ?raw import`).toBeGreaterThan(100);
    }
  });

  it("every <Dialog> in each file carries padding=\"roomy\", at the expected count", () => {
    for (const [name, src, count] of FILES) {
      const openings = dialogOpenings(src);
      expect(openings.length, `${name}: unexpected number of <Dialog> openings`).toBe(count);
      for (const tag of openings) {
        expect(tag, `${name}: a <Dialog> is missing padding="roomy"`).toContain('padding="roomy"');
      }
    }
  });

  it("adds no other padding variant (no accidental default/none/flush opt-out)", () => {
    for (const [name, src] of FILES) {
      for (const tag of dialogOpenings(src)) {
        expect(tag, `${name}: <Dialog> should only use padding="roomy"`).not.toMatch(
          /padding="(?:default|none)"/,
        );
      }
    }
  });

  it("the extractor + matcher still bite (meta-guard, literals built by concat)", () => {
    // A footer with inner JSX `>`s but NO padding must be caught, and the extractor must stop
    // at the tag's own close rather than swallowing children.
    const missing = "<Dialog" + " open footer={<>x</>}>body</Dialog>";
    const tag = dialogOpenings(missing);
    expect(tag).toHaveLength(1);
    expect(tag[0]).toBe("<Dialog" + " open footer={<>x</>}>");
    expect(tag[0]).not.toContain('padding="roomy"');

    // And a compliant one is recognised.
    const present = "<Dialog" + ' open padding="roomy">body</Dialog>';
    expect(dialogOpenings(present)[0]).toContain('padding="roomy"');

    // `<ConfirmDialog` is not a `<Dialog`.
    expect(dialogOpenings("<ConfirmDialog" + " open>x</ConfirmDialog>")).toHaveLength(0);
  });
});
