import { expect, it } from "vitest";
import manifest from "../../package.json";
import lock from "../../../../package-lock.json";

it("uses matching published UI packages with the collapsed SideNav and overlay Escape APIs", () => {
  const ui = lock.packages["node_modules/@protolabsai/ui"];
  const css = lock.packages["node_modules/@protolabsai/ui-css"];
  expect(manifest.dependencies["@protolabsai/ui"]).toBe(manifest.dependencies["@protolabsai/ui-css"]);
  expect(ui.version).toBe(css.version);
  for (const [name, entry] of [["ui", ui], ["ui-css", css]] as const) {
    const [major, minor] = entry.version.split(".").map(Number);
    expect(major > 0 || minor >= 73).toBe(true);
    expect(entry.resolved).toBe(`https://registry.npmjs.org/@protolabsai/${name}/-/${name}-${entry.version}.tgz`);
    expect(entry.integrity).toMatch(/^sha512-/);
  }
});
