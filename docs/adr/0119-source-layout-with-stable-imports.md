# ADR 0119 — Organize runtime source without renaming imports

- Status: Accepted
- Issue: [#4178](https://github.com/protoLabsAI/protoAgent/issues/4178)

## Context

Twenty runtime packages occupy the repository root alongside applications,
assets, tools and documentation. Renaming them into a single Python namespace
would affect thousands of imports, plugin integrations, dynamic imports and
patch targets without providing additional root-directory cleanup.

## Decision

Move runtime packages into `src/`, preserving their existing import names and
the `protoagent` / `python -m server` entry points. Hatch strips the source
prefix when building wheels. `uv sync` installs the project editably; each
worktree needs its own environment. `evals/` and bundled `plugins/` retain their
root locations, as do configuration seeds, static assets and the applications.

Read-only assets resolve through `infra.paths._app_root()`: the repository root
for source installs, site-packages for wheels, and `_MEIPASS` for frozen builds.
Source recognition is bounded to the immediate parent of `src/` and checks the
project manifest; it never searches arbitrary ancestor directories or the cwd.
Instance data continues to use `instance_paths()` independently of source layout.

Launchers that deliberately run uninstalled source put both `src/` and the root
on `PYTHONPATH`. PyInstaller explicitly searches both roots and retains existing
bundle destinations. Source scanners walk `src/<package>` and derive import
names relative to `src/`; missing scan roots must not silently pass.

## Consequences

- Root navigation becomes smaller without an import compatibility migration.
- A fresh checkout requires `uv sync` before normal module or CLI execution.
- Editable installs still expose the root for evals and bundled plugins; this
  migration does not claim complete isolation from other root Python files.
- Wheel and frozen smoke checks must run outside the checkout, without its
  `PYTHONPATH`, so editable installs cannot mask missing packages or assets.
- Existing layering contracts retain their module names and meaning.

The epic is integrated through reviewed slices. The operator tests the combined
epic before it merges to main. Namespace changes, plugin relocation and cosmetic
deployment/document moves are separate future work.
