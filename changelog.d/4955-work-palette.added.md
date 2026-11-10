- **Work hub in the command palette (#4955).** ⌘⇧K now lists "Open Work" plus a row per
  Work section (Decisions, Goals, Watches, Tasks, Schedules, Background agents, Activity,
  Directives) and "Work: New goal…", each opening the Work surface in the large shell modal
  on the matching section. They route through a serializable NavIntent, so they work from the
  desktop launcher too.
  On mobile, these commands navigate to Work. New goal opens the creation form, and closing
  it clears the request so returning to Work does not reopen it.
  Opening Work from any dock transfers its mounted view to the modal, preventing duplicate
  goal creation dialogs when Work lives in the left or bottom dock.
  Dock to rail reveals Work in its assigned dock, restores it when hidden, and retains the
  selected section. Collapsed docks reopen at a usable size.
