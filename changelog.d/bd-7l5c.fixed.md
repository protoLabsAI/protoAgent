- **Fleet groups and pins no longer fail silently, and the switcher hides its write controls on a hub without the groups API (#4735).**
  The Fleet settings group editor (#4735) and the agent switcher assumed the hub served the
  groups/pins API (#4731). Against an older hub that predates it, the group and pin routes 404/405
  and the write was swallowed — "clicking Add group does nothing", and a pin star or a Move to group
  updated the screen then silently reverted on the next poll. The console now detects a hub whose
  `GET /api/fleet` returns no `groups` field: the switcher hides the pin star and the row menu's Pin
  and Move to group (rendering the plain Ungrouped + Remote list, keeping the per-browser filter and
  recents), and the Groups editor shows an update note instead of the editor. When a group or pin
  write is rejected the optimistic change is now rolled back and the failure is reported — the hub's
  own message for a rejected name, otherwise "Couldn't save that change. Try again." — never a silent
  swallow.
