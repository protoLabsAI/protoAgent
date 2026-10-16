// Pins the DS type scale (#3688) at its source: the @protolabsai/design bump to ^0.10.0 is
// the FOUNDATION every downstream font-size migration card depends on, so this guard fails
// loudly if the design package is ever downgraded below the minor that ships the scale, or if
// a shipped step changes value. It reads the design package's own tokens.json (the same file
// PluginView derives its --pl-* set from) rather than any console CSS — no source CSS/TS is
// touched by this card, and there is deliberately no local token definition to assert against.
import designTokens from "@protolabsai/design/tokens.json";
import { describe, expect, it } from "vitest";

import { PL_TOKEN_VARS } from "./PluginView";

// The scale steps the migration cards will move sites onto, and the px value each must carry.
// Keys are the tokens.json `font.size.*` leaves; the derived custom property is
// `--pl-font-size-<step>` (PluginView's kebab-case flatten of the key path). `display` (28px) is
// the large heading step added in @protolabsai/design 0.13.0 (picked up here with the ds-5j6 DS
// release); the other seven are the original #3688 scale.
const TYPE_SCALE: ReadonlyArray<readonly [step: string, px: string]> = [
  ["3xs", "10px"],
  ["2xs", "11px"],
  ["xs", "12px"],
  ["sm", "13px"],
  ["base", "14px"],
  ["lg", "16px"],
  ["xl", "18px"],
  ["display", "28px"],
];

describe("DS type scale — @protolabsai/design ^0.10.0 ships --pl-font-size-* (#3688)", () => {
  const fontSize = (designTokens as { font: { size: Record<string, string> } }).font.size;

  it.each(TYPE_SCALE)("font.size.%s is %s", (step, px) => {
    expect(fontSize[step]).toBe(px);
  });

  it("exposes exactly the scale steps it declares, no more, no fewer", () => {
    expect(Object.keys(fontSize).sort()).toEqual(TYPE_SCALE.map(([step]) => step).sort());
  });

  it("surfaces each step as a --pl-font-size-* var, so the theme bridge forwards it to plugins", () => {
    // consoleTheme() posts PL_TOKEN_VARS onto a plugin iframe's :root (ADR 0026 bridge), so a
    // step missing here would ship the token to CSS but never reach embedded views.
    for (const [step] of TYPE_SCALE) {
      expect(PL_TOKEN_VARS).toContain(`--pl-font-size-${step}`);
    }
  });
});
