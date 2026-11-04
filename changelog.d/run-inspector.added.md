- **A run inspector drawer in the console (#PRNUM).**
  A View run link under each chat answer, on each row of the Telemetry recent turns table,
  on each background job and in the Org Chart's node drawer opens one drawer showing
  everything recorded for that run. Its header gives the outcome, the total cost, the
  wall-clock span and the completion check's verdict. Below it is the tree of delegations,
  each with its kind, target, outcome, cost, duration and completion badge, the error of a
  failed one (a tap shows all of it), and the turns recorded for it with links to their
  traces and prompts. Work still running shows a live dot and its progress, and the drawer
  refreshes every three seconds until nothing is in flight. A `?run=<id>` link opens the
  drawer directly. `GET /api/runs/{id}` now also resolves the uuid of any delegation inside a
  run, finished or still running, and returns the trace link template. Plugin views can open
  the drawer with the new `protoagent:run:open` bridge message.
