- **Record the private steering dev-instance results and outstanding fixes.**
  The proposal's P1h.5 note (2026-11-18) records steps 1, 4 and 5 PASS at
  `10f042a83`, with no canary leak. Step 2 (parked resume) FAILED and is fixed by
  bd-kaztd: a private steer into a session parked on a decision it may not answer
  is refused, not started beside it. Step 3 (restart) FAILED: a private steer sent
  while a public turn ran, then a kill and restart, came back FAILED ("Task
  interrupted by an agent restart; its runner did not survive.") and restart
  recovery found nothing, because no live route writes a durable pending turn.
  Nothing leaked. The restart defect is being fixed under protoAgent#5504
  (card bd-upbbz). Steps 2 and 3 are rerun on the dev instance after both fixes
  merge (bd-v8szk). `private_steering.enabled` stays default off pending Josh's
  separate go.
