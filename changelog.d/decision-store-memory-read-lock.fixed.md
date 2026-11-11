- **A flaky decisions test no longer fails CI.** The in-memory decisions store that tests use
  shares one database connection across threads; two reads at once could raise "bad parameter
  or other API misuse". Its reads now take the same lock as its writes. Production stores open a
  connection per call and are unaffected.
