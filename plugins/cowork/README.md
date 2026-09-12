# Cowork — the knowledge-work skill pack

The skill pack behind protoAgent's **Cowork archetype**
([ADR 0083](../../docs/adr/0083-cowork-mode-archetype.md)) — for knowledge workers
graduating from Claude Cowork to a self-hosted agent whose deliverables are real files.

Skills plus **one** goal/watch verifier (`cowork:folder_changed`): no tools, routes, views,
surfaces, MCP servers, config, secrets, or events. Deliverables land in the operator's
fenced work folders (Settings ▸ Tools). An old config that still sets `cowork.output_dir`
is ignored: that key was never read, and it was removed after 0.4.0.

## Skills

| Skill | What it does |
|---|---|
| `docx` / `xlsx` / `pptx` / `pdf` | Produce and edit Word/Excel/PowerPoint/PDF deliverables via `execute_code` + the standard Python libraries |
| `/daily-brief` | The day on one page — schedule with prep flags, who's waiting on you, work in flight, heads-ups — as a styled HTML artifact; schedulable on any cron cadence |
| `schedule` | Distill the current session into a self-contained prompt and run it on any cron cadence or one-shot |
| `drop-folder` | Turn a fenced folder into a drop zone — a watch (backed by this plugin's `cowork:folder_changed` verifier) notices arrivals/edits/deletions and runs a distilled follow-up |
| `consolidate-memory` | Reflective maintenance pass over long-term memory — merge, retire, sharpen |
| `writing-voice` | Learn the operator's voice from samples they share; saved as a `my-writing-style` skill |
| `/setup-cowork` | Guided first-run: folders → import existing Claude Code/Cowork state (via claude-bridge) → connect tools → try a skill → voice → first schedule |

## On by default

Bundled and **on by default**, like `notes` / `docs` / `artifact` / `craft`: every agent
gets the document skills and habits with no setup step. The cost is ten entries in the
always-on `<available_skills>` index. To turn it off on one instance:

```yaml
plugins:
  disabled: [cowork]
```

An explicit disable removes the pack entirely: no skills, no verifier, zero bytes in the
prompt.

Being on also turns on [`execute_code`](../execute_code/) (the manifest's
`enables: [execute_code]`), because the four document skills produce files through it.
An explicit `plugins.disabled: [execute_code]` still wins, and disabling cowork returns
execute_code to its own default (off) unless you enabled it yourself. Only its default
changes: its subprocess, scrubbed environment, timeout, bridge allowlist and enforcement
gate are exactly as before.

The document skills need [`execute_code`](../execute_code/); on the desktop app that means
the one-click [managed Python runtime](../../docs/guides/python-runtime.md), whose document
baseline (`python-docx`, `openpyxl`, `python-pptx`, `reportlab`, `pypdf`) already covers
this plugin's `requires_pip`. On a server, the console's **install-deps** action installs
them into the runtime. `save_file_artifact` (the [`artifact`](../artifact/) plugin, on by
default) is what turns a produced file into a versioned download card.

## It used to live in its own repo

This pack shipped as `protoLabsAI/cowork-plugin` through v0.3.1 and moved in-tree in
#3450 so it is maintained with the host it runs on. The manifest's `supersedes` names
that repo, so an already-installed git copy stands down in favour of this one with its
enabled state, config and secrets intact — see
[the plugin registry guide](../../docs/guides/plugin-registry.md#when-a-plugin-moves-into-core-supersedes).
The bundled version must stay **above** every release of the retired repo (#1574).

## Licensing note (ADR 0083 D3)

These document skills are **original, clean-room work** over `python-docx`, `openpyxl`,
`python-pptx`, `pypdf`, and `reportlab`. Anthropic's Cowork skills are all-rights-reserved
and are not used here; a test tripwire
(`test_no_anthropic_licensed_material_vendored` in `tests/test_cowork_plugin.py`) keeps it
that way.

## Tests + evals

- **Tests:** `python -m pytest tests/test_cowork_plugin.py -q` from the repo root. Most of
  the suite is host-free — it drives `register()` and the verifier against a fake registry,
  the way the standalone repo's CI did.
- **Evals:** `evals/tasks.json` holds live behavioral cases
  ([ADR 0012](../../docs/adr/0012-eval-strategy-and-model-comparison.md)) — the document skills produce
  real files through `execute_code`, `/daily-brief` consults memory and renders an artifact
  (and is *not* volunteered for a plain calendar question), and a recurring ask round-trips
  through `schedule_task`/`cancel_schedule`. Run them against an instance that has this
  plugin, `execute_code` and the doc deps:

  ```
  python -m evals.runner --tasks-file plugins/cowork/evals/tasks.json
  ```

  Reports land in `evals/results/`, model-tagged like the core suite, so `evals/report.py`
  trends them across runs. The in-repo tests only guard the file's shape; the cases
  themselves need the live agent.
