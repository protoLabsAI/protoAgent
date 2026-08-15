- **Trust-matcher hardening from the #2733/#2735 review findings (#2721).** The
  prefix fallback in the trust matcher (and the byte-identical installer allowlist)
  widened every exact entry into a bare-`*` glob — acking `github.com/x/y` silently
  trusted `github.com/x/y-evil`, and an allowlisted `github.com/org` admitted
  `github.com/org-evil`; both now widen only at a path boundary (`/*`).
  `ssh://git@…` spellings normalize correctly (scheme and `git@` strip together), a
  string `"false"` for `plugins.trust_unverified` no longer reads as *enable* the
  don't-ask switch, and `peek_bundle` refuses a manifest member id that isn't a
  single safe path component — a `..`-bearing id could previously resolve outside
  the peek's temp directory before the fetch wrote.
