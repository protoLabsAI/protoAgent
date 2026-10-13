# Enable document creation on desktop

To create Word, Excel, PowerPoint, and PDF files in the desktop app, install its
managed Python runtime. This supplies the interpreter and libraries used by
`execute_code` and the Cowork document skills.

## Install it for this app {#install-it-once-per-machine}

1. Open **Settings → Tools**.
2. Click **Install runtime** and wait for the download and document-library
   installation to finish.
3. Wait for **Python runtime installed** and confirm the install card clears.
   Then [create a small document](/guides/documents-and-files#create-a-document).
   If the tool still reports a missing runtime, check the install status for an error.
   Use **Retry install** after resolving a failed download or installation.

Installation downloads a pinned, hash-verified CPython and installs the document
libraries. It starts only when requested. The runtime is shared by agents on the
same box; it does not need reinstalling for each agent.

You can also install and inspect it from the CLI, using the same box root as the
desktop app:

```bash
protoagent runtime install-python
protoagent runtime list
```

Source and Python-package runs use their existing Python interpreter. They do
not need the desktop runtime; install any missing document dependencies in that
environment instead.

## What needs it

The `execute_code` plugin and skills that call it need the interpreter. Cowork's
archetype warns during setup if it is missing. If a tool call happens first, its
error points to **Settings → Tools** rather than starting a download.

## How you find out before something fails

**Tools** shows a warning dot for a missing runtime, stale document libraries,
or failed install. The install card shows progress while working. An unsupported
platform displays a notice; document execution cannot run through the managed
runtime on that build.

## The baseline can go stale

A release can change the document-library pins without changing Python itself.
When that happens, choose **Update runtime** in **Settings → Tools**. It updates
the libraries and keeps the interpreter. The baseline includes PDF-reading
libraries as well as office-document writers.

For the packaging design, see [Managed Python runtime](/adr/0094-managed-python-runtime)
and [Document artifacts](/adr/0092-desktop-document-baseline-and-versioned-file-artifacts).

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
