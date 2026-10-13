// DS 0.63 card 3g: the three edge-to-edge dialogs must opt into the DS Dialog
// `padding="none"` body padding (`.pl-dialog__body--flush`) while keeping their scoped
// className. Today they render flush only because scoped CSS counter-overrides the
// app-wide 24px body padding (settings.css `.settings-overlay`/`.theme-quick-dialog`,
// goals.css `.goal-create-modal`). A later card deletes those `padding: 0` declarations
// along with the global rule; declaring `padding="none"` on the component now keeps them
// edge-to-edge once that happens. While both exist the scoped rule wins, so this renders
// identically. Vite `?raw` imports the real source text (mirrors dialogPadding.test.tsx):
// this tsconfig has no node types, so we don't reach for `node:fs`.
import { describe, expect, it } from "vitest";

import goalsSrc from "./GoalsPanel.tsx?raw";
import settingsOverlaySrc from "../settings/SettingsOverlay.tsx?raw";
import themeQuickSrc from "../settings/ThemeQuickButton.tsx?raw";

const CASES: Array<{ file: string; src: string; className: string }> = [
  { file: "SettingsOverlay.tsx", src: settingsOverlaySrc, className: "settings-overlay" },
  { file: "ThemeQuickButton.tsx", src: themeQuickSrc, className: "theme-quick-dialog" },
  { file: "GoalsPanel.tsx", src: goalsSrc, className: "goal-create-modal" },
];

// Extract the source of each `<Dialog …>` OPENING tag. Multi-line tags and props whose
// values contain `>` inside a `{…}` expression or a string are handled by tracking brace
// depth and string state, so we stop at the `>` that actually closes the opening tag.
function dialogOpeningTags(src: string): string[] {
  const tags: string[] = [];
  const re = /<Dialog\s/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    let i = m.index + "<Dialog".length;
    let brace = 0;
    let quote: string | null = null;
    for (; i < src.length; i++) {
      const c = src[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") quote = c;
      else if (c === "{") brace++;
      else if (c === "}") brace--;
      else if (c === ">" && brace === 0) break;
    }
    tags.push(src.slice(m.index, i + 1));
  }
  return tags;
}

describe("edge-to-edge dialogs opt into DS flush body padding (card 3g)", () => {
  for (const { file, src, className } of CASES) {
    it(`${file} <Dialog> passes padding="none" and keeps className="${className}"`, () => {
      const tags = dialogOpeningTags(src).filter((t) => t.includes(`className="${className}"`));
      expect(tags.length, `expected a <Dialog className="${className}"> in ${file}`).toBe(1);
      expect(tags[0]).toContain('padding="none"');
    });
  }
});
