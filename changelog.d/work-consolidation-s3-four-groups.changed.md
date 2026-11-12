- **Work shows the four approved groups (#5100).** The Work view's Overview and picker now
  follow the approved consolidation: Needs you, Doing now, Coming up and Your instructions,
  with one count each, help lines and inline empty copy per the spec. Rows and counts come
  from the shared `workGroups` selector, rows carry kind tags and icons (Decision / Delivery /
  Goal stopped; Goal / Job; Time / Condition / Job; Instruction), a stopped goal reads neutral,
  finished items leave Work, and not-started jobs sit only in Coming up. Legacy section deep
  links (palette rows, chat strips, pills) resolve into their canonical group; the per-kind
  detail renderers and the third-pass responsive layout are unchanged. The activity history
  section is gone from Work — the Feed pill opens its own Feed dialog for pending-page triage
  and the completed-turn timeline.
