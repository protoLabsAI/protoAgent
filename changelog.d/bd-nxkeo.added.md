- **Directives close with their work: links and auto-fulfil (bd-nxkeo, ADR 0129 D4/Q2).**
  `record_directive` accepts optional `links` — GitHub issue/PR refs (`owner/repo#N` or
  issue/PR URLs), board card ids (`bd-…`), scheduler job ids, task ids — stored on the
  directive. When every linked item reaches a terminal state (issue/PR closed or merged,
  card done or cancelled, job finished, task closed) the directive is fulfilled
  automatically, `closed_by: "system"`, with an attributed reason
  ("acme/widgets#879 closed; acme/widgets#873 closed"). The live paths: `task.changed`
  closes, the scheduler's own post-settlement `scheduler.changed` publishes (a one-shot
  fired and removed, a cron expired on max_fires/TTL, a cancel from the console or the
  `cancel_schedule` tool — which now publishes it), and a cheap periodic `gh` sweep for
  the GitHub refs. Card refs resolve through a new generic `task.link.closed` bus topic
  the auto-fulfil subscribes to — an inbound seam for anything that can report a ref
  finished (nothing in core publishes it yet; a board plugin can, when a card goes
  done/cancelled).
  The `directive.changed` publish makes the fulfilled directive leave
  Work's "Your instructions" and drop the rail's "Told it N" live. Unlinked directives
  behave exactly as before.
