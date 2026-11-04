- **Fleet settings has a group editor for the agent switcher (#4731, #4732).**
  Settings ▸ Fleet now has a Groups view (docs/design/fleet-switcher-groups.md) that organizes the
  agent switcher into sections — the console half of the groups + pins the roster already stores.
  Each operator group is a card holding its agents, in switcher order, followed by a "No group"
  card and a read-only Remote card. Add a group (its name field opens ready to type), rename it
  inline (Enter saves, Escape cancels, with the spec's error copy for a blank, duplicate or
  reserved "Remote" name), reorder groups by the drag handle or the keyboard-reachable Move up /
  Move down, and delete one — its agents move to No group, none is removed. Move an agent between
  groups by dragging it onto another card or by the Group picker on its row, and pin or unpin any
  agent, remote included. Every change saves at once with a small "Saved" note and no Save button;
  the writes are the same hub roster fields the switcher reads, so a pin or a group set here shows
  in the switcher too. Remote agents are listed read-only with the machine they're on. On a phone
  it's one column with the drag handles off, the Group picker on its own line and 44px targets. The
  editor is read-only in a directly-reached member window, like the rest of Fleet settings.
