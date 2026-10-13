// DS 0.63 card 3e: every content dialog in schedule/delegates/new-agent/pair-remote
// must opt into the DS Dialog `padding="roomy"` body padding BEFORE a later card deletes
// the app-wide `.pl-dialog__body { padding }` rule (apps/web/src/app/theme.css). While the
// global rule still exists these opt-ins render identically; once it's gone, omitting one
// would silently shrink that dialog's body padding from 24px to the DS default 16px.
// This test guards the opt-in on each `<Dialog` in the four files. Vite `?raw` imports the
// real source text (mirrors dialogPadding.test.tsx from card 3d): this tsconfig has no node
// types, so we don't reach for `node:fs`.
import { describe, expect, it } from "vitest";

import delegatesSrc from "./DelegatesSection.tsx?raw";
import newAgentSrc from "./NewAgentPanel.tsx?raw";
import pairRemoteSrc from "./PairRemoteDialog.tsx?raw";
import scheduleSrc from "../schedule/SchedulePanel.tsx?raw";

const FILES: Array<[string, string]> = [
  ["SchedulePanel.tsx", scheduleSrc],
  ["DelegatesSection.tsx", delegatesSrc],
  ["NewAgentPanel.tsx", newAgentSrc],
  ["PairRemoteDialog.tsx", pairRemoteSrc],
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

describe("content dialogs opt into DS roomy body padding", () => {
  for (const [name, src] of FILES) {
    it(`${name} passes padding="roomy" to every <Dialog>`, () => {
      const tags = dialogOpeningTags(src);
      expect(tags.length).toBeGreaterThan(0);
      for (const tag of tags) {
        expect(tag).toContain('padding="roomy"');
      }
    });
  }
});
