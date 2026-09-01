// Which of a plugin's DECLARED views the console actually mounts as a navigable surface.
//
// A manifest's `views:` list is not the console's surface list. Two declared kinds never
// become one: a `slot: "chat"` claimant (ADR 0045) renders under the core `chat` rail id
// instead of getting its own, and a `utility` widget (2026-06 IA pass) is a bottom-left
// pill that opens a DIALOG — neither is reconciled into `railOrder`, so neither has a
// `plugin:<id>:<view>` surface to navigate to.
//
// Shared because THREE places need the same answer and drifting apart is a live bug, not a
// tidiness concern: App's rail derivation, the desktop launcher's copy of it, and the
// palette-command adapter's allow-set for `navigate`/`open_view`. The adapter is why this
// is a function rather than a filter written out a third time — it inherited the raw
// `views` list, so a declared `navigate` at a utility widget compiled a live row that set a
// surface id nothing renders, and App's stale-surface fallback then yanked the operator to
// chat (#3294 review).
export function isNavigablePluginView(view: { slot?: unknown; utility?: unknown } | null | undefined): boolean {
  return !!view && view.slot !== "chat" && !view.utility;
}
