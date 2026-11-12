- **The chat shows its live work as one status rail above the composer (work consolidation
  v2 §5, S6a).** The loose goal / background-job / standing-instruction strips are replaced
  by shared-count chips — `Doing now · goal 3/8 · N`, `Coming up N`, `Told it N` (numbers
  only on the phone, with complete spoken labels) — counted by the same `workGroups`
  selector the Work view reads, scoped to this chat, so the chip numbers and the Work group
  rows cannot drift. Not-started jobs count in Coming up, never Doing now; zero-count chips
  hide and an empty rail takes no space. Each chip opens one grouped details panel (a
  popover on desktop, a bottom sheet on the phone) with one section per Work group and an
  "Open in Work" deep link; the instructions ledger keeps its failure state with a "Load
  again" retry, judged against this chat's own ledger — another chat's failed ledger never
  marks this chat's instructions out of date, and this chat's failure is never hidden by
  other chats having loaded. One polite live region announces a tied job's completion once ("<job>
  finished. 1 approval it carried is now fulfilled."). The people chips keep their
  mention-toggle behavior unchanged.
