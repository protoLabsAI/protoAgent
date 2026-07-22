# 0094 — Managed Python runtime: execute_code on the frozen desktop app

Status: **Proposed** (design-first; supersedes nothing — completes ADR 0092's stated
outcome and closes [#2137](https://github.com/protoLabsAI/protoAgent/issues/2137))

## Context

`execute_code` never registers on the packaged desktop app: a hard gate on
`sys.frozen` (`plugins/execute_code/__init__.py`) plus an in-tool refusal
(`plugins/execute_code/engine.py::run_code`). The mechanism is sound — the engine
runs model-authored code via `create_subprocess_exec(sys.executable, …)`, and in a
PyInstaller build `sys.executable` *is* the frozen server binary, so there is
nothing to spawn. The consequence is [#2137](https://github.com/protoLabsAI/protoAgent/issues/2137):
**any capability that computes through code is silently inert on the flagship
distribution.** Concretely, cowork's docx/xlsx/pptx/pdf skills all author documents
*through* `execute_code`, so [ADR 0092](./0092-desktop-document-baseline-and-versioned-file-artifacts.md)
D1 (bundle the doc libs) made cowork *install* on desktop but not *produce* — "the
desktop app can't make a .docx" survived #2123.

Two facts constrain the fix:

1. **An interpreter alone is not enough.** PyInstaller packs pure-Python libraries
   into the PYZ archive inside the binary — they are not loose files an external
   interpreter can import. The D1-bundled `python-docx`/`openpyxl`/`python-pptx`/
   `reportlab` are reachable only from *inside* the frozen process (which is why the
   ADR 0092 D2 preview extractors work). A spawned child cannot import them.
2. **The child's library surface has always been the interpreter's own
   site-packages.** The engine spawns with a scrubbed env (`PATH` + pipe FDs only, no
   `PYTHONPATH`), so from source the child sees the venv's site-packages — that is
   the semantic a desktop fix must reproduce, not extend.

We already shipped the acquisition pattern this needs:
[ADR 0085](./0085-managed-node-runtime.md) provisions a **pinned, hash-verified Node
on demand** into the box-shared `box_root/runtime/node/current` (`runtime/node_install.py`
download → checked-in `_SHA256` verify → guarded extract → atomic swap;
`infra/node_runtime.py` as the light discovery leaf; `operator_api/node_routes.py`
for the console; `protoagent runtime install-node` for the CLI).

## Decision

Provision a **managed CPython runtime** with the ADR 0085 mechanics, and make it the
`execute_code` child interpreter on frozen builds.

- **D1 — Acquisition.** `runtime/python_install.py` + `infra/python_runtime.py`,
  mirroring the Node pair file-for-file: a pinned **CPython 3.12.x** from
  python-build-standalone (`install_only` archives, ~35 MB), an in-repo SHA256 table
  keyed by `(platform, arch)` (darwin-arm64/x64, linux-x64/arm64, windows-x64 —
  real integrity gate, not trust-on-first-use), member-guarded extraction
  (`filter="data"` + absolute/`..` rejection), atomic swap into
  `box_root/runtime/python/current` with the old install kept until the new one is
  in place, `_VERSION_MARKER` + `python_status()` for status surfaces. Box-tier for
  the same reason Node is: one machine provisions once; every instance and fleet
  member shares it. 3.12 matches the frozen sidecar's interpreter line, minimizing
  behavior drift against source runs.
- **D2 — Interpreter resolution.** `engine.run_code` resolves the child interpreter:
  frozen → the managed python (actionable refusal when not provisioned — see D4);
  source → `sys.executable`, unchanged. **No system-Python fallback on frozen**:
  a discovered user Python of arbitrary version with arbitrary site-packages
  reproduces #2137's failure class (silently missing capability) with worse
  debuggability. Deliberately narrower than Node's "a user's own install wins" —
  `npx` needs *a* Node; `execute_code` needs a *known* runtime with a known library
  surface.
- **D3 — Child libraries.** Provisioning pip-installs the **doc baseline** into the
  managed runtime's own site-packages, from
  `apps/desktop/sidecar/requirements-docs.txt` — the same single source of truth
  ADR 0092 D1 bakes into the bundle — using the runtime's bundled pip
  (`-m pip install --only-binary=:all: -r …`). This reproduces the source-run
  semantic exactly (child imports = interpreter's site-packages) and closes the
  cowork gap end to end. Posture: the interpreter is hash-pinned; the wheels ride
  normal pip/PyPI trust, the same trust any server/Docker install already extends —
  wheel hash-pinning lands with the ADR 0093 family if/when we want it.
- **D4 — Honest state everywhere (folds in #2137's "make it visible" — as part of
  the fix, not a bandaid).** On frozen + supported platforms the plugin now
  *registers*; the "unavailable" state becomes explicit instead of invisible:
  the tool result and the Settings ▸ Tools copy say "managed Python runtime not
  provisioned — install it under Settings ▸ Tools (~35 MB, one time)" instead of
  today's copy that implies the toggle works; the plugin list and archetype catalog
  surface `python_status()`. Unsupported platforms keep a clear terminal message.
- **D5 — Surfaces.** `GET /api/runtime/python` + `POST /api/runtime/python/install`
  (202 + progress, mirroring `operator_api/node_routes.py`), CLI
  `protoagent runtime install-python`, and a one-click install button beside the
  execute_code toggle in Settings ▸ Tools. Enabling execute_code on desktop with no
  runtime present prompts the download (consent = the click; the plugin itself
  remains **disabled by default** per [ADR 0071](./0071-plugin-trust-and-consent.md)).
- **D6 — Explicitly out of scope.** (a) PATH exposure of the managed python — Node
  augments PATH because many consumers need `npx`; this runtime is scoped to the
  `execute_code` spawn only, no ambient interpreter. (b) Superseding
  ADR 0093 (#2131) — complementary, not competing: 0093 puts wheels on the **frozen
  host process's** `sys.path`; 0094 gives the **child** an interpreter + its own
  site-packages. Together they are the desktop compute tier. (c) Sandboxing changes —
  the subprocess + scrubbed env + hard timeout posture is exactly today's; this ADR
  moves *where the interpreter comes from*, not what the code may do.

## Phases

- **P1 (this track):** `infra/python_runtime.py` + `runtime/python_install.py` (+
  tests mirroring `tests/test_node_runtime.py`), engine interpreter resolution +
  registration change + honest copy, doc-baseline install from
  `requirements-docs.txt`, `python_routes.py` + CLI + Settings button. Acceptance:
  a stock desktop install, after one consented click, produces a real `.docx` via
  cowork's skill; source-run behavior byte-identical to today.
- **P2:** plugin-declared child deps — an enabled plugin's `requires_pip` can be
  installed into the managed runtime (per-plugin console action / manifest hint), so
  third-party compute plugins stop being desktop-dead too.
- **P3 (deferred):** convergence with ADR 0093 (shared pin/lock format for host-side
  and child-side deps); optional PATH exposure if a second consumer materializes.

## Alternatives considered

- **In-process execution on frozen** — rejected. Three concrete regressions, not a
  posture wash: model-authored code would read `os.environ` **with credentials**
  (the scrubbed child env is load-bearing); a C-extension crash takes the whole
  agent down instead of a child; and a CPU-bound script cannot be reliably killed
  in-process, so the hard-timeout guarantee dies.
- **First-party fixed document tools over the bundled libs** — honest stopgap,
  wrong shape: docs-only (the issue's blast radius is the capability tier), requires
  rewriting cowork's skills against a fixed API (their value is arbitrary
  composition in code), and adds N tools to maintain.
- **Discover a system Python** — reproduces the invisible-breakage class (version
  drift, deps roulette); rejected per D2.
- **Bundle an interpreter in the installer** — ~35–45 MB for every user including
  the majority who never enable execute_code, a second binary to notarize; the
  same reasoning that shaped ADR 0085's on-demand answer.

## Consequences

- Cowork's document half actually runs on the distribution it targets — ADR 0092's
  stated outcome ("installs **and works**") becomes true; #2137's headline dies.
- One new binary surface in the box root, hash-pinned at acquisition, shared
  box-wide; one-time ~35 MB consented download.
- `execute_code` stays opt-in and disabled by default; its trust story is unchanged
  and now honestly presented on desktop.
- The desktop/source behavior matrix collapses: the child's library surface is the
  interpreter's site-packages in both worlds.
