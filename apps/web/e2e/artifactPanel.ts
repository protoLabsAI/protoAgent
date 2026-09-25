import type { Page } from "@playwright/test";

// The Artifact plugin for the artifact-ref chip specs (#3617): the mock's runtime status
// doesn't list it (adding a right-dock view there would shift every other spec's rail), so a
// spec that needs the panel adds it for its own page only — the mock server is shared by
// parallel workers. The panel's store is a three-version chain (`art-chain`, one svg per
// version with a distinguishable id) served on /history, with /refs computed off the same data.

const svg = (n: number) => `<svg id="v${n}" xmlns="http://www.w3.org/2000/svg" width="80" height="40"><text x="4" y="24">v${n}</text></svg>`;

export const CHAIN_STORE = {
  current: "art-chain",
  artifacts: [
    {
      id: "art-chain",
      kind: "svg",
      title: "Signal chart",
      versions: [1, 2, 3].map((n) => ({ code: svg(n), ts: n, by: "agent" })),
      version_count: 3,
      created: 1,
      updated: 3,
    },
    {
      id: "art-other",
      kind: "svg",
      title: "Other",
      versions: [{ code: '<svg id="other" xmlns="http://www.w3.org/2000/svg"/>', ts: 1, by: "agent" }],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

export const ARTIFACT_PLUGIN_STATUS = {
  id: "artifact",
  name: "Artifact",
  version: "0.19.0",
  enabled: true,
  loaded: true,
  tools: ["show_artifact", "update_artifact", "rewrite_artifact"],
  skills: 1,
  views: [{ id: "artifact", label: "Artifact", icon: "Sparkles", placement: "right", path: "/plugins/artifact/view" }],
};

export async function withArtifactPanel(page: Page) {
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: CHAIN_STORE }));
  await page.route("**/api/plugins/artifact/refs?*", (route) => {
    const ids = (new URL(route.request().url()).searchParams.get("ids") || "").split(",");
    const artifacts: Record<string, unknown> = {};
    for (const a of CHAIN_STORE.artifacts) {
      if (!ids.includes(a.id)) continue;
      artifacts[a.id] = { title: a.title, kind: a.kind, version_count: a.version_count, oldest: 1 };
    }
    return route.fulfill({ json: { artifacts } });
  });
}
