# 0116 — A local-first data analyst: DuckDB over files in place, charts as Vega-Lite specs, and a plugin-service call seam

- Status: Proposed
- Date: 2026-10-03
- Builds on: [ADR 0038](./0038-generative-ui-artifacts-two-mode.md) (the Artifact panel and its sandboxed frame), [ADR 0092](./0092-desktop-document-baseline-and-versioned-file-artifacts.md) (file artifacts), [ADR 0039](./0039-plugin-event-bus.md) (plugins talk without importing each other), [ADR 0043](./0043-plugin-consumption-sdk-workflows-extraction.md) (the consumption SDK), [ADR 0019](./0019-plugin-config-settings-secrets.md) §3b (`spawns: true`, the operator-only setting fence).
- Refs: the `chart my week` launch demo (~50 s from send to chart); #4019 (the vendoring + nonce-CSP pattern this reuses).

## Context

The Data Analyst is a local-first agent shape: *your agent, your data, your way*. The operator points it at data that already sits on their disk — CSV, Parquet, JSON, Excel and SQLite files, or a folder of them — and the agent explores it, answers questions in SQL, and puts charts in the console. Nothing is uploaded, and nothing is imported into a database first.

Three things are missing today.

**1. There is no data engine.** The agent can read a file with `read_file`, or write Python with `execute_code`. Neither is a good way to answer "what were my best weekdays last quarter?" over 100k rows. The first floods the context window. The second makes the model author a pandas program for every question and is only as safe as the code it writes.

**2. Charts are slow because the model draws them by hand.** The only way to put a chart in the panel is a `react` (or `html`) artifact. The model writes a whole component: imports, layout, axis formatting, colours, and the data itself typed out as a JavaScript literal. In the launch demo, `chart my week` took about 50 s from send to chart, and almost all of that time was the model generating that component. A chart is not a component. It is a *query* plus a *mapping* from columns to marks. Vega-Lite expresses that mapping in a few hundred bytes of JSON.

**3. A plugin cannot get a result back from another plugin.** The data plugin has to create an artifact and report which one it made. Plugins coordinate over the event bus (ADR 0039), which is fire-and-forget: no return value, and no way to ask "is that plugin even on?". The only other route is to import the artifact plugin's internals (`plugins.artifact._tools._show`). That is the coupling the plugin contract exists to prevent: it breaks on any refactor, and it fails at import time when the plugin is disabled. No plugin creates artifacts today (checked at write time: the only cross-module reader is `server/chat_session_ops.py`, which imports `resolve_for_bundle` defensively). So no existing seam can be reused.

## Decision drivers

- **Time to chart.** The model should write the least it can (a SQL query and a small spec) and the host should do the rest.
- **Read-only, enforced by the engine.** The agent's SQL must not be able to write, attach, install, load, or read anything outside what the operator allowed. A regex over SQL text is not a fence.
- **Operator-owned scope.** Only the operator decides which folders the agent can read. The agent cannot widen that scope itself, even with `set_config`.
- **Offline and same-origin.** Charts render with no network, like mermaid and the pptx renderer (#4019).
- **The console's look.** A chart should look like the console it sits in, in both dark and light themes, without the model choosing any colours.
- **Plugins stay decoupled.** A plugin must never import another plugin's internals.

## Options considered

### Engine

| Option | For | Against |
|---|---|---|
| **DuckDB, embedded (chosen)** | Queries CSV, Parquet and JSON *in place*, with no import step. It is columnar and fast on a laptop, and installs as one MIT-licensed wheel with no server. It has engine-level lockdown settings: `enable_external_access`, `allowed_paths`, `lock_configuration`. | It cannot read SQLite or Excel without extensions that `INSTALL` from the network (D2). |
| SQLite (stdlib) | Already present. | Every file has to be imported first, its analytics functions are weak, and it has no file-scanning lockdown. |
| pandas / polars in `execute_code` | Flexible. | The model writes a program for every question, and the only fence is the sandbox. Neither library is a host dependency. |

### Chart format

| Option | For | Against |
|---|---|---|
| **Vega-Lite spec + inline rows (chosen)** | A declarative grammar of graphics: a chart is about 300 bytes of JSON that models already write well. Vega can be themed from tokens, has tooltips, and runs offline with no `eval` (D5). | About 830 KB of vendored JS. |
| React + chart.js (status quo) | Already vendored. | It is the slow path that motivated this ADR: the model writes the component and the data. |
| ECharts / Plotly specs | Popular. | Plotly is about 3.5 MB. Both are less constrained, and Plotly can load remote resources unless configured carefully. Models write them less reliably. |

### Cross-plugin seam

| Option | Against |
|---|---|
| Import `plugins.artifact` internals | It is the coupling the plugin contract forbids. It breaks on any refactor and on a disabled plugin. |
| Event bus request/reply | The bus is fire-and-forget by design (ADR 0039). A reply would need correlation IDs, timeouts, and a second topic, which reinvents an RPC badly. |
| A dedicated `sdk.show_artifact()` in core | It would make `graph/` depend on one plugin, and every later plugin-to-plugin need would want its own SDK function. |
| **Plugin services (chosen)** | — a generic, small, named-callable registry (D6). |

## Decision

### D1 — It ships as a plugin first: `data` (Data Analyst), in its own repo

The analyst is `protoLabsAI/data-plugin` (plugin id `data`, MIT, off by default), with the same conventions as `campaign-plugin`: a host-free test suite, CI, and the shared release workflow. It has seven tools:

| Tool | Does |
|---|---|
| `data_connect(path, name)` | Registers a file, or a folder of files (bounded walk), as named sources. SQLite gives one source per table and Excel one per sheet. |
| `data_sources()` | Lists sources with their kind, path, row count and column count. |
| `data_schema(source)` | Shows columns, types and sample rows. |
| `data_query(sql)` | Runs a read-only SELECT and returns a compact table, with row and time caps. |
| `data_profile(source)` | Reports per-column nulls, cardinality, min/max/mean/stddev, top values, and IQR outliers. |
| `data_chart(sql, vega_lite_spec, title)` | Runs the query, inlines the rows into the spec, and hands it to the Artifact panel (D5, D6). |
| `data_export(sql, format)` | Writes CSV or Parquet into the agent's workspace (D7). |

It also ships two skills: `exploring-a-dataset` and `building-a-chart` (the spec patterns that read well). A "Data Analyst" archetype bundle (catalog row, SOUL, default skills) comes later, and its scope is an open question.

### D2 — DuckDB, embedded, queries files where they are

Each source is a DuckDB **view** over its file (`read_csv` / `read_parquet` / `read_json_auto`), so a query reads the operator's file directly and nothing is copied. The wheel has only `core_functions`, `icu`, `json` and `parquet` built in. Reading SQLite or Excel would need `INSTALL sqlite`/`excel`, which is a network fetch of native code, and D3 refuses that by design. So those two formats are **snapshotted to Parquet** in the plugin's private cache:

- SQLite through the stdlib `sqlite3`, opened read-only.
- Excel through `openpyxl`, an optional MIT dependency.

The snapshot is taken on a private connection that the agent's SQL never reaches. It is re-taken when the source's mtime or size changes, and that check runs at query time.

### D3 — Read-only is enforced by the engine, and the SQL text is checked as a second layer

**Every query gets a fresh in-memory connection, configured in this order:**

1. `autoinstall_known_extensions=false` and `autoload_known_extensions=false`, set at connect time.
2. A private `temp_directory`. DuckDB adds its temp directory to `allowed_directories` automatically, and the default temp directory is relative to the process working directory.
3. `memory_limit` and `threads`.
4. `allowed_paths` set to the **exact resolved files** of the currently valid sources, plus their Parquet snapshots. This setting cannot be passed at connect time ("before the database is started"), so it is a `SET` here.
5. `enable_external_access=false`.
6. One view per source.
7. `lock_configuration=true`.

The result was verified against DuckDB 1.5.6. Under this configuration the engine itself refuses all of the following:

- reads outside the allowlist, including a sibling `.env` via `read_text` and `glob()`
- `COPY … TO`, including onto an allowed path
- `ATTACH` of a file
- `EXPORT DATABASE`
- `INSTALL` and `LOAD`
- `SET` and `RESET` of any option
- `https://` URLs

`getenv()` does not exist in the embedded build.

**Statement guard (defence in depth).** `conn.extract_statements(sql)` must return exactly **one** statement of type `SELECT`. This closes what the engine still allows: an in-memory `ATTACH ':memory:'`, `CREATE TABLE`, and `PRAGMA`. It also refuses multi-statement batches.

**Caps.**
- Rows: `fetchmany(cap + 1)`, with truncation reported to the model.
- Wall time: a timer calls `conn.interrupt()`.
- Memory: `memory_limit`.

The defaults are 200 rows, 20 s and 1 GB, and they are plugin settings.

### D4 — The scope is the operator's `data_dirs`, an operator-only allowlist

`data_dirs` is a plugin setting marked **`spawns: true`**, the same marker campaign uses for `upload_dirs`. It is core's only per-key operator-only fence (ADR 0019 §3b), so the agent's own `set_config` cannot widen it. The fence is campaign's upload fence:

- Symlinks are resolved **first**, and the real path must then be inside a root, so a link inside a root cannot point out of it.
- `..` is normalised away by that same resolution.
- Hardlinked files are refused, because a hardlink dodges every name check.
- Credential directory names (`.ssh`, `.aws`, …) and credential file names (`.env`, `*.pem`, `id_rsa`, …) are refused even inside a root.
- The agent's home (`~/.protoagent`, `$PROTOAGENT_HOME`) is refused.
- A root that is the filesystem root, the home directory or a parent of it, or the agent home is ignored as too broad.

An empty `data_dirs` (the default) refuses every connect, and the refusal says where the operator sets it. Every source is **re-validated at query time**. A source whose file has left the fence, or whose root was removed from the setting, drops out of `allowed_paths` and is reported. A connect is not a permanent grant.

### D5 — Charts are a new core artifact kind, `vega-lite`

`show_artifact(kind="vega-lite", code=<spec JSON>)` renders a Vega-Lite spec **with its data inline** (`data.values`). The data plugin's `data_chart` builds that spec: the model writes the encoding, and the tool inlines the query's rows.

- **Vendoring.** `vega` 6.4.0, `vega-lite` 6.4.3 and `vega-embed` 7.3.0 (all BSD-3-Clause) are vendored as their own UMD builds, byte-for-byte. They are SRI-pinned in the shell's `LIB` map, served same-origin from the allowlisted `/plugins/artifact/vendor/` route with CORS for the opaque sandbox, and kept out of eol conversion by the existing `plugins/artifact/vendor/** -text`. The notices for everything the builds bundle are in `vendor/vega.LICENSES.txt`.
- **Sandbox.** The chart runs in the same no-same-origin frame as every artifact, under a **nonce CSP with no network**: `default-src 'none'`, nonce-only scripts, `connect-src 'none'`, `img-src`/`font-src` limited to `data:` (and `blob:` for images), and no workers, frames, objects, base or form targets. The injected base scripts (the ask bridge and the error boot) carry the nonce.
- **No `eval`.** Vega compiles expressions with `Function` by default. Here it runs as `vega-interpreter`, which vega-embed 7.3 bundles and selects with `ast: true`, so the CSP never needs `'unsafe-eval'`.
- **Loader lockdown.** Vega's loader is replaced with one that **refuses every load** (`load`/`sanitize`/`http`/`file`), so `data.url`, a spec loaded by URL, and image-mark URLs cannot fetch. Also, vega-embed merges a spec's own `usermeta.embedOptions` *over* the caller's options, loader included. The frame strips that key before embedding. `actions: false` removes the export/editor menu, whose editor action opens a remote site.
- **Feedback before render.** A spec that is not a JSON object, or that contains a `url` anywhere inside a data definition, is refused when the version is written: on create, update and rewrite, with a reason the model can act on. The frame re-checks all of it. The server-side check is feedback, and the frame is the fence.
- **Theme.** The chart is themed from the console's own data-viz tokens: `--pl-color-chart-series1…8` become the categorical range, `--pl-color-chart-axis` and `--pl-color-chart-grid` style the guides, and `--pl-color-fg`, `-fg-muted`, `-bg` and `--pl-font-sans` style the text and ground. A single view fills the panel width (`width: "container"`, `fit-x`). A live theme switch re-draws the chart in the new palette, with the tooltip theme following the ground's luminance. A spec's own `config` still overrides the theme.
- **Render verdict.** It is reported from the embed promise, not from the `load` event, because a chart can still fail after load. Dataflow errors reach the verdict through a logger.

### D6 — Plugin services: a named-callable seam for plugin-to-plugin calls

- **Provider side.** `registry.register_service(name, fn, description="")` offers `fn` as `<plugin_id>.<name>`. The name is namespaced by the provider's ID (the same rule as `emit`), lowercase, and refused when malformed. A duplicate keeps the first registration.
- **Consumer side.** `graph.sdk.service(name)` resolves the callable **at call time**, or returns `None` when no loaded plugin provides it. `None` is the normal answer for a disabled plugin, a plugin that is not installed, or a core that is too old. Consumers degrade with an actionable message. A service is an optional capability, never a hard dependency.
- **Wiring.** The loader aggregates services into `PluginLoadResult.services`. `server/plugin_wiring._apply_plugin_registries` rebinds the live table (`graph/plugin_services.py`) **wholesale** at build and on every reload, the same way verifiers and work providers are handled (#1752), so a disabled provider stops resolving immediately. The operator-MCP process runs that same function, so a tool running under ACP resolves services exactly as it would in the main process.
- **Testkit.** `testkit.FakeRegistry.register_service` captures services and raises on what the host would refuse.

The first service is **`artifact.show(kind, code, title="") -> dict`**. It is `show_artifact` without code links: the same kinds, cap, checks, version chain and render-verdict wait. It returns `{ok, id, version, message, ref}`, and refusals are returned as data, never raised. `ref` is the `artifact-ref` chat-chip tail, which the caller appends last so the chat shows a chip that opens the chart.

### D7 — Exports go only to the agent's workspace

`data_export` writes to `<workspace>/data-exports/` (`infra.paths.workspace_dir()`) and never to a source directory. The output is refused if the workspace itself sits inside a `data_dirs` root.

- **Filename.** A bare name: path separators and `..` are refused.
- **How it writes.** A dedicated connection whose `allowed_paths` are the sources plus the *exact* output file runs `COPY (<the already-validated single SELECT>) TO …`.

## Consequences

- **Faster charts.** A chart becomes a query plus a few hundred bytes of spec, instead of a component with the data typed into it. The send-to-chart time measured end to end is recorded in the core PR.
- **Engine-enforced read-only.** Both layers of the read-only guarantee are covered by plugin tests: write, `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `SET`, `PRAGMA`, multi-statement batches, reads outside `data_dirs`, and symlink, `..` and hardlink escapes.
- **Vendored weight.** The artifact plugin gains about 830 KB of vendored JS. It loads only in a chart frame and is cached immutable.
- **Plugin services are a public API.** That raises the bar on providers: a service's signature and return shape are documented in its docstring and kept backward compatible. The plugin API reference pages document `register_service` and `sdk.service`.
- **Snapshots cost disk.** SQLite and Excel sources are snapshots, not live reads: a large SQLite database costs a Parquet copy in the plugin cache, and a write to the database is picked up at the next query, through mtime/size.
- **Version floor.** The data plugin's `min_protoagent_version` is the core release that ships this ADR's kind and seam.

## Open questions (operator calls)

- **Naming.** Is it "Data Analyst" or "Analyst"? The name matters most for the archetype and catalog row; the plugin can keep the ID `data` either way.
- **Archetype scope.** What does the Data Analyst bundle add on top of the plugin? Options include a SOUL, the two skills plus a "weekly metrics brief", a default `data_dirs` prompt as a bundle `config_input` on `data.data_dirs`, and a dashboard view that pins several charts.
- **Dashboards.** Several pinned charts with shared filters would mean either a `vega` (full grammar) kind or a dashboard artifact. That decision is deferred until there is a real use case.
