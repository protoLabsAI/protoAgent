- **Spend-cap status reports the daily notice limits.** `GET /api/telemetry/spend-cap` now
  also returns `daily_cost_soft_limit` and `daily_token_soft_limit`, so the console can show
  where the notice limit sits and when unattended spend has passed it. A value of 0 means no
  notice limit is set. The existing fields and the pause behaviour are unchanged. Refs #5540.
