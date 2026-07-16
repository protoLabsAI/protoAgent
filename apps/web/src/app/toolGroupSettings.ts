// Settings that belong to a TOOL GROUP (#2000).
//
// The Tools panel already groups the assembled toolset by subsystem category — the same
// categories the backend stamps on each tool (`_tool_category` in console_handlers.py).
// Some of those groups have config that governs the group as a whole: whether its tools are
// bound at all, and how they're allowed to run. That config is the group's, so it lives ON
// the group — expanded in place from the group's header — instead of in a separate dialog
// floating above a panel whose every other control is inline.
//
// (It previously hung off a `<QuickSetting>` chip at the top of the panel, which read as a
// second, redundant settings surface — and the dialog rendered its fields with a plain map,
// skipping the `depends_on` visibility the canonical settings pages honour, so dependent
// gates showed even when their parent was off and controlled nothing.)
//
// Keyed by the category string the backend sends on each tool, so a group only grows a gear
// when there's genuinely something to configure. Add an entry to give another group one.

/** Dotted settings keys for a tool group, in render order. Empty = no group settings. */
const TOOL_GROUP_SETTINGS: Record<string, string[]> = {
  // ADR 0007 operator primitives — the fenced project fs toolset and its shell gate.
  // Order matters: each is `depends_on` the one above it (enabled → allow_run →
  // run_requires_approval → bypass_allowed), so they render as a narrowing chain and
  // dependents hide until their parent is on.
  Filesystem: [
    "filesystem.enabled",
    "filesystem.allow_run",
    "filesystem.run_requires_approval",
    "filesystem.bypass_allowed",
  ],
};

/** The settings keys for `category`, or `[]` when that group has none. */
export function toolGroupSettingKeys(category: string): string[] {
  return TOOL_GROUP_SETTINGS[category] ?? [];
}

/** True when this group has settings worth a gear in its header. */
export function toolGroupHasSettings(category: string): boolean {
  return toolGroupSettingKeys(category).length > 0;
}
