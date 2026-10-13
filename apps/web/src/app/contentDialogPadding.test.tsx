// DS 0.63 card 3c: every content dialog in the mcp-catalog / docviewer / knowledge /
// memory surfaces must opt into the DS Dialog `padding="roomy"` body padding BEFORE a
// later card deletes the app-wide `.pl-dialog__body { padding }` rule
// (apps/web/src/app/theme.css). While the global rule still exists these opt-ins render
// identically; once it's gone, omitting one would silently shrink that dialog's body
// padding from 24px to the DS default 16px. This test guards the opt-in on each
// `<Dialog` in the four files — including the Suspense/fetch-driven surfaces that are
// costly to render standalone in jsdom. Vite `?raw` imports the real source text
// (mirrors plugins/dialogPadding.test.tsx from card 3d): this tsconfig has no node
// types, so we don't reach for `node:fs`.
import { describe, expect, it } from "vitest";

import mcpCatalogSrc from "./McpCatalogDialog.tsx?raw";
import documentViewerSrc from "../docviewer/DocumentViewer.tsx?raw";
import knowledgeStoreSrc from "../knowledge/KnowledgeStore.tsx?raw";
import memorySurfaceSrc from "../memory/MemorySurface.tsx?raw";

const FILES: Array<[string, string]> = [
  ["McpCatalogDialog.tsx", mcpCatalogSrc],
  ["DocumentViewer.tsx", documentViewerSrc],
  ["KnowledgeStore.tsx", knowledgeStoreSrc],
  ["MemorySurface.tsx", memorySurfaceSrc],
];

// Extract the source of each `<Dialog …>` OPENING tag. Multi-line tags and props whose
// values contain `>` inside a `{…}` expression or a string are handled by tracking brace
// depth and string state, so we stop at the `>` that actually closes the opening tag.
// `<ConfirmDialog` (imported alongside `Dialog` in some files) never matches — the regex
// requires `<` immediately before `Dialog`.
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

  it("KnowledgeStore.tsx has both its dialogs covered", () => {
    // The ingest ("Add a source") and add-entry ("Add a knowledge entry") dialogs.
    expect(dialogOpeningTags(knowledgeStoreSrc).length).toBe(2);
  });
});
