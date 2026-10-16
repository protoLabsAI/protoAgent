- **Background delegation progress keeps a fixed footprint while it runs (#4246).** A live
  `DelegateProgressView` no longer bounces taller/shorter on nearly every frame: the tool list
  holds a constant 1 + RECENT_SHOWN row slots (missing ones are reserved, aria-hidden empties),
  the narration tail is always present and CSS-clamped to two lines with a reserved height, and a
  long plan renders a fixed-size window around the in-progress entry plus a "+N more" line. The
  settled/done view is unchanged.
