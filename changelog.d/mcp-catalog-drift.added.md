- **The MCP quick-add catalog is now checked against upstream every week (#2910).** An
  entry in Settings ▸ MCP ▸ Browse only runs when someone clicks it, so a package that
  upstream renamed or pulled used to fail in front of an operator and nowhere else. The
  catalog once pointed at a renamed sequential-thinking package for weeks. A scheduled
  check now confirms every entry's npm package or PyPI project still resolves, isn't
  deprecated or yanked, that remote endpoints answer and docs links resolve. It files one
  tracking issue when something drifts, and it also runs on any PR that edits the catalog.
