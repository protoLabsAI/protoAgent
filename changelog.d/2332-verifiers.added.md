- **`GET /api/verifiers` — every verifier a goal or watch can use, from every source
  (ADR 0028/0067).** The console's goal creator hardcoded its verifier list in TypeScript, and
  the new watch creator inherited the copy. Two things followed: the list drifted from the
  server registry (`plugin` was missing from it entirely), and because `plugin` was missing,
  **every plugin-contributed check was unreachable from the console** — even though the
  operator API has always accepted them. On a machine with the usual plugins installed that
  silently excluded `spacetraders:credits`, `careercoach:new_matches`,
  `learning_wiki:strength` and friends, which are precisely the checks worth watching, since
  they read real state instead of judging a transcript.

  The endpoint enumerates the live registries and labels each entry with its `source`
  (`core` / `plugin`); both creators build their pickers from it. Choosing `plugin` reveals a
  picker of the registered checks plus an optional JSON `args` field. The `plugin` option is
  hidden entirely when nothing registers a check, so the picker is never empty, and the form
  falls back to the core types if the fetch fails.

  `register_goal_verifier` gained an optional `description` so a contributed check can explain
  itself instead of showing a bare `<plugin-id>:<name>`. Additive — a plugin that omits it is
  attributed by its namespace.
