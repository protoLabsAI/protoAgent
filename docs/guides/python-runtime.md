# Managed Python runtime (desktop)

On the **packaged desktop app** the server is a frozen binary — `sys.executable` *is*
protoAgent, not a Python interpreter — so [`execute_code`](/guides/plugins) has nothing
to run child code with. The **managed Python runtime**
([ADR 0094](../adr/0094-managed-python-runtime.md)) fixes that: a one-click, consented
download of a pinned CPython that the desktop app owns, plus the **document baseline**
([ADR 0092](../adr/0092-desktop-document-baseline-and-versioned-file-artifacts.md)) —
the libraries that let document skills produce real `.docx` / `.xlsx` / `.pptx` / `.pdf`
files.

**Source runs never need this.** A `python -m server` / `uv run` instance spawns its own
interpreter; every status surface reports `needed: false` and stays hidden. This page is
desktop-only behavior.

## What needs it

- **`execute_code`** — the in-tree Python-interpreter plugin. On a frozen build its tool
  registers either way, but calls return an actionable "runtime isn't installed yet"
  notice until you provision.
- **Everything routed through `execute_code`** — above all the **Cowork document skills**
  (docx / xlsx / pptx / reportlab-PDF). This is why the Cowork archetype declares
  `requires: [python_runtime]` and the new-agent picker warns at choose-time
  (see [Fleet § archetypes](/guides/fleet)).

## Install it (once per machine)

Two equivalent paths — both fetch a **hash-verified CPython 3.12.13** (the frozen
sidecar's own interpreter line, ~35 MB) and then pip-install the document baseline
(`apps/desktop/sidecar/requirements-docs.txt`) into the runtime's own site-packages:

- **Console** — **Settings ▸ Tools** shows an install card while the runtime is missing:
  one click, live progress (download → document libraries), done. The card renders
  nothing once the runtime is present and current.
- **CLI** — `protoagent runtime install-python` (and `protoagent runtime list` shows
  `python: not provisioned — …` / version + baseline state).

The download is a deliberate consent point — ~130 MB on disk after the baseline lands —
so nothing auto-provisions.

## How you find out before something fails

- **Settings nav badge** — the **Tools** entry carries a warning dot whenever the
  runtime needs attention (not provisioned, stale baseline, failed install; pulsing
  while an install runs), so the state is visible while browsing, not mid-task.
- **Archetype choose-time warning** — picking an archetype that declares
  `requires: [python_runtime]` (Cowork) shows a notice under the card grid when this
  host's runtime isn't ready.
- **Actionable tool copy** — an `execute_code` call on an unprovisioned build returns
  the fix ("Settings ▸ Tools, or `protoagent runtime install-python`") instead of a
  bare error.

## The baseline can go stale

The runtime records a hash of the `requirements-docs.txt` it installed. When a release
changes the document pins, the status flips to `baseline_current: false` and the
surfaces above offer an **update** (re-runs the pip phase only) — the runtime itself
stays put.

## Status & API

`GET /api/runtime/python` returns `{python, install}`:

| Key | Meaning |
|---|---|
| `needed` | this process would use it (frozen builds only) |
| `managed` / `managed_version` / `exe` | a working install is present, its version, its interpreter path |
| `baseline_installed` / `baseline_current` | document-library state vs the current pins |
| `supported` / `target_version` | can this platform/arch provision, and what an install fetches |

`POST /api/runtime/python/install` starts the provisioning in the background (`202`;
poll the GET for phase + percent). Unsupported platform/arch combinations return the
banner state instead — `execute_code` (and the skills behind it) can't run on that
desktop build.
