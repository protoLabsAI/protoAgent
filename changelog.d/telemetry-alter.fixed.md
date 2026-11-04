- **A failed schema migration in a telemetry store raises instead of passing as done (#PR).**
  When the telemetry, delegation ledger or prompt snapshot store opened an older DB, it
  added missing columns with an ALTER and treated any SQLite error as "column already
  present". A lock timeout during a real migration therefore let the store start without
  the column, and every insert failed until the process restarted. The stores now read
  the table's columns once and ALTER only the missing ones. An ALTER that fails raises,
  except when a concurrent opener added the same column first.
