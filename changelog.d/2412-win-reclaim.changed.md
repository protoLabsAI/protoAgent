- **More of the test suite runs natively on Windows CI (#2412).** Two files
  (`test_instance_paths`, `test_store_tier_resolvers`) came off the Windows-native
  exclusion burndown — one needed a forward-slash path assertion made separator-agnostic,
  the other was already portable after the ADR 0098 process-tree migration. The Windows PR
  job now gates them too (exclusion ledger 18 → 16).
