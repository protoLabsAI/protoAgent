- **Feed pill: a plain unread count — the priority-now alert badge, label and info text
  are gone (bd-048fa).** Feed redesign slice 6 (docs/design/feed.md §2, §9.5, §10): the
  utility-bar Feed pill's badge is now the bare DS `Count` (`9+` past nine), shown only
  while there are unread items — the `activity-badge--alert` tint, `data-alert`
  attribute and the `!` fallback are removed, and a priority-now `inbox.item` bumps the
  count exactly like any other item (anything operator-class pages through Work › Needs
  you, bd-048fa slice 4/tone.ts — never the pill). The label reads `Feed — N new` / `Feed`
  and the hover info `N new item(s) since you last looked` / `Feed — what <agent> was
  asked to do and what it did, newest first`; no "priority-now page" wording remains.
  Unread still counts both `inbox.item` and `activity.message` while the dialog is
  closed, opening clears it, and the pill still opens the Feed's own dialog at
  `UTIL_DIALOG_WIDTH` — never the Work modal.
