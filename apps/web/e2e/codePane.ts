import type { Page } from "@playwright/test";

// The code pane is an opt-in toolset (ADR 0112 amendment, `filesystem.code_pane`, default
// off), reported to the console as `/api/runtime/status` `code_pane.enabled`. The mock serves
// it OFF (the server default); a spec that exercises the pane turns it on for its own page
// only — the mock server is shared by parallel workers, so no global flip.
//
// `state.enabled` is read per request, so a spec can flip it mid-test and watch the console
// pick the change up on its next status fetch (a settings save invalidates that query).
export async function withCodePane(page: Page, state: { enabled: boolean } = { enabled: true }) {
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, code_pane: { enabled: state.enabled } } });
  });
  return state;
}
