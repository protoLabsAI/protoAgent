- **Install deps runs one pip at a time per environment (#3450).** Two clicks, two tabs, two
  plugins' installs, or the CLI beside the console could each run pip into the same
  environment at once, and one install's post-install refresh could re-import a package
  another was still writing. Now a second install into the same environment is refused at
  once with a 409 that names the one running. That covers this server's Python and the
  desktop app's shared managed runtime, and it holds across processes: the dev and default
  instances share one venv, and fleet members share the managed runtime. The Plugins panel
  shows the running row as installing and makes every other row's Install deps wait, including
  for an install started in another tab. The setup wizard's report does the same.
- **Discover shows a bundled plugin's real state, and never offers Install for one (#3450).** A
  plugin that ships in core now says "bundled · on" or "bundled · off" instead of a bare
  "bundled". When another bundled plugin turned it on, it says so: execute_code is "on because
  cowork enables it". "Bundled" now follows the installer's own built-in rule. A leftover
  folder from a plugin that moved out of core no longer hides its Install button, and a bundled
  plugin whose folder name differs from its id is recognised.
- **`cowork.output_dir` is gone (#3450).** Nothing ever read it, so the document skills pointed
  at a folder setting the agent had no way to see. Deliverables still go to the operator's
  fenced work folders. A config that still sets the key is ignored, without a warning.
