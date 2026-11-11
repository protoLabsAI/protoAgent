- **`scripts/desktop-install-candidate.sh` puts a release-candidate desktop build on your Mac.**
  It reuses or dispatches a `desktop-build` test run for main's head (or a given commit or run),
  verifies the signed DMG, swaps `/Applications/protoAgent.app`, keeps the previous app, and
  records the commit it came from. `--restore` puts the previous app back.
