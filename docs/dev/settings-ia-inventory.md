# Settings IA — inventory of what exists today

**Status:** current-state record. Descriptive, not aspirational — this documents the settings
surface *as it is* at v0.102.0, so the rework has a baseline it can be checked against.
The target organisation is a separate doc: [settings-ia-target.md](./settings-ia-target.md).

Generated against `graph/settings_schema.py` + `apps/web/src/settings/`. Regenerate the tables
after any schema change (the counts below are asserted in `docs/dev/` review, not by CI).

---

## 1. How the surface is assembled

Three independent layers decide where a setting shows up. Nothing checks that they agree.

1. **`Field.section`** (`settings_schema.py`) — a free-string label declared per field. Groups
   fields into accordion sections *within* a panel.
2. **`_SECTION_CATEGORY`** (`settings_schema.py:~1100`) — maps a section → a category.
   **`_category_for()` defaults any unmapped section to `"Plugins"`.**
3. **The console** (`SettingsSurface.tsx`) — decides which categories get a schema-driven
   `SettingsCategoryPanel`, which get a bespoke panel, and which get neither.

A field is reachable only if all three line up. When they don't, the failure is **silent** —
there is no "unmapped section" error, and `SettingsSurface` resolves an unknown section as
`sections.find(...) ?? sections[0]`, rendering an unrelated panel rather than erroring.

### Console sections today (`SettingsSurface.tsx`)

| Group | Section | Renders |
|---|---|---|
| Agent | Identity | `IdentityPanel` (bespoke: name + SOUL via `/api/config`) |
| Agent | Operator & access | `SettingsCategoryPanel category="Identity"` |
| Agent | Model | `SettingsCategoryPanel category="Model"` |
| Agent | Behavior | `SettingsCategoryPanel category="Behavior"` |
| Agent | Knowledge | `SettingsCategoryPanel category="Knowledge"` |
| Agent | Secrets | `SecretsPanel` → `category="Secrets"` + status card |
| Agent | Integrations | `PluginSettingsHome` (per-plugin dialogs → `category="Plugins"`) |
| Capabilities | Tools / MCP / Skills / Subagents / Delegates | **bespoke managers — no schema panel** |
| Box | Overview / Fleet / Telemetry | **bespoke panels — no schema panel** (host console only) |
| This console | Theme / Chat / Keyboard / Developer | device-local, own backends |

**Categories with a schema panel:** Identity, Model, Behavior, Knowledge, Secrets, Plugins.
**Categories without one:** `Capabilities`, `Box` — 20 fields between them.

### The chip escape hatch

ADR 0048 §2.2 describes `<QuickSetting>` as *"a shortcut to the canonical field, same save path"*.
In practice it is the canonical home for most fields that use it, because Capabilities and Box
have no panel to be a shortcut *to*:

| Chip | Where | Keys | Also has a canonical home? |
|---|---|---|---|
| Recall | Knowledge store | `knowledge.top_k`, `knowledge.embeddings` | **yes** — Settings ▸ Knowledge |
| Box runtime | Fleet panel | `network.bind`, `fleet.port_base`, `fleet.discovery.{port_min,port_max,mdns}`, `fleet.warm.{max,grace_seconds}` | no |
| Shell & filesystem tools | Tools panel | `filesystem.{enabled,allow_run,run_requires_approval,bypass_allowed}` | no |
| Skill sharing | Skills panel | `skills.scope`, `commons.path` | no |
| MCP server sharing | MCP panel | `mcp.scope` | no |
| Telemetry | Telemetry panel | `telemetry.enabled`, `telemetry.retention_days` | no |

**2 of 19 chip-covered fields are actually shortcuts. 17 are the only door.**

Note the precedent already set in `SettingsSurface.tsx:65-67` — Identity's schema fields were
pulled *out* of a chip into their own section because *"a chip-in-a-dialog was unnecessary extra
clicking."* Capabilities and Box never got that treatment.

---

## 2. Defects found in the current surface

Each is verified, not inferred. Severity is about operator impact.

### D1 — Three settings are unreachable from any UI (**high**)

Declared, cascade correctly, save fine via `/api/settings`, appear nowhere in the console.
They are **not** `ui_hidden` — this is an accident, not a decision.

| key | section → category | why it's stranded |
|---|---|---|
| `egress.allowed_hosts` | Network → Box | Box has no panel; the Box-runtime chip omits it |
| `fleet.autostart` | Keep-warm → Box | same; chip covers the other two Keep-warm fields |
| `telemetry.fleet_trace_export` | Telemetry → Box | same; chip covers the other two Telemetry fields |

`egress.allowed_hosts` is an outbound network allowlist — a security control that can only be
set by hand-editing YAML.

Contrast with the four *deliberately* hidden fields (`ui_hidden=True`, filtered at
`settings_schema.py:1200`): `goal.enabled`, `soul.self_edit_enabled`, `middleware.enforcement`,
`identity.name`. Those are intentional and documented at their declaration.

### D2 — Two independent controls for the same tool, never reconciled (**high**)

| Control | Writes | Effect |
|---|---|---|
| Tools ▸ "Allow run_command" (chip) | `filesystem.allow_run` | the tool is never **built** |
| Tools ▸ `run_command` row switch | `tools.disabled: [run_command]` | the tool is **dropped** after assembly |

Both mean "the agent has no shell." Nothing couples them: turn the row off and the chip still
reads *"Allow run_command: on"*. The same overlap exists between `filesystem.enabled` and the
eight Filesystem row switches. The build-time vs denylist distinction is real in the backend and
invisible to the operator.

### D3 — `filesystem.enabled: false` deletes its own off-switch (**high**)

Verified live against a dev instance:

```
filesystem.enabled: true  → GET /api/tools → 8 tools, category "Filesystem"
filesystem.enabled: false → GET /api/tools → 0 tools, none listed as disabled
```

`tools.disabled` keeps a tool **listed but `enabled:false`** precisely so the console can toggle
it back on (`console_handlers.py` says so). `filesystem.enabled:false` means the tools are never
built — so they appear in neither `bound_tools` nor `disabled_tools`, and the Filesystem group
disappears from the panel entirely.

**Consequence for the rework:** any design that puts the filesystem settings *inside* the
Filesystem tool group is a one-way door — turning the toolset off removes the group containing
the switch that turns it back on. Recovery would be hand-editing YAML. The panel-level chip,
whatever else is wrong with it, does not have this failure mode.

### D4 — `QuickSettingDialog` ignores `depends_on` (**medium**)

`SettingsCategory` filters rows through `fieldVisible()` (`visibility.ts`); `QuickSettingDialog`
maps over its fields raw. So dependent gates render even when their parent is off and they
govern nothing — e.g. "Require approval per command" showing while `run_command` is off. The
dialog also drops the inheritance badges, override notes, and reset-to-inherited that the
canonical pages provide.

### D5 — Unmapped sections silently become "Plugins" (**medium**)

`_category_for()` defaults to `"Plugins"`. Two **core** sections are unmapped and therefore file
themselves under Agent ▸ Integrations:

| section | fields | actually is |
|---|---|---|
| `Media` | `media.public`, `media.retention_days` | the core media store (`registry.save_media`, #1929) |
| `Persona` | `soul.self_edit_enabled` | core persona / SOUL (ADR 0081) |

A new core section with no map entry lands in Integrations with no warning.

### D6 — Near-duplicate section names, one of them a single field (**medium**)

Behavior contains both:

- **`Agent runtime`** (2 fields) — `agent_runtime`, `operator_mcp.tools`
- **`Runtime`** (1 field) — `runtime.autostart_on_boot` ("Install/remove the boot LaunchAgent")

Two sections a word apart, one holding a single machine-level toggle that has nothing to do with
the agent runtime.

### D7 — Two "autostart on boot" settings in different categories (**low**)

| key | category | means |
|---|---|---|
| `runtime.autostart_on_boot` | Behavior | install a **macOS LaunchAgent** for this instance |
| `fleet.autostart` | Box (section *Keep-warm*) | which **fleet members** the hub restarts on boot |

Not redundant in behaviour, but identically named in the UI and filed far apart. Separately,
`fleet.autostart` is filed under **Keep-warm**, which is about LRU eviction caps — boot policy
isn't keep-warm.

### D8 — One domain split across two categories (**medium**)

| domain | pieces | lives in |
|---|---|---|
| Skills | `skills.scope`, `commons.path` | Capabilities |
| | `skills.top_k` | **Knowledge ▸ Recall** |
| Filesystem | `filesystem.*` (4) | Capabilities |
| | `operator.project_dir`, `operator.allowed_dirs` (the dirs the fs/tasks/notes APIs may touch) | **Identity** |

`skills.top_k` also has an **empty description**.

### D10 — a failed rebuild leaves disk and runtime diverged (**high — not an IA bug**)

The settings write is **not atomic**. `_apply_settings_changes` commits the config to YAML, *then*
rebuilds the graph. When the rebuild fails, the config stays written and the process keeps serving
the **old** graph — with only a toast to say so.

Reproduced live on a dev instance with no gateway key:

```
POST /api/settings {"agent_runtime":"acp:proto"} → ok:true  "reloaded"
POST /api/settings {"agent_runtime":"native"}    → ok:false
   messages: ["config saved", "graph rebuild failed: Missing credentials …"]

langgraph-config.yaml  → agent_runtime: native      ← committed
GET /api/config        → agent_runtime: acp:proto   ← still running the old graph
```

Disk and runtime now disagree, silently. And the next restart boots `native` — the state the
rebuild just rejected — so a failure that was survivable in-process becomes a **fatal boot**.
That isn't hypothetical; it was verified by restarting the same instance:

```
graph/agent.py:912   llm = create_llm(config)
graph/llm.py:238     return _ReasoningChatOpenAI(**kwargs)
openai.OpenAIError: Missing credentials …
→ process exits 1. The instance will not start.
```

**A toast-level warning ended in an instance that cannot boot**, recoverable only by hand-editing
`langgraph-config.yaml` — which is exactly what it took to restore it. The console offers no way
back, because the console needs the server that won't start.

Why the runtime swap specifically: `create_llm` (`graph/llm.py:207`) carries an ACP-only fallback —
under `acp:*` **with no gateway configured**, aux calls route through the ACP agent, so no
credentials are needed. `native` has no such fallback. An ACP-only instance is therefore *one
settings save away from being unbootable*, and the UI presents that save as an ordinary dropdown
change.

Note the rebuild path is careful *internally*: it closes the freshly-built MCP clients and commits
no scheduler state (`agent_init.py:1633-1639`, and `tests/test_reload_rebuild_deps.py` asserts
"nothing committed"). The gap is that the **YAML write happens earlier** and isn't part of that
rollback. `tests/test_config_secrets.py:312` documents an earlier instance of the same symptom
("this is what failed live"), fixed for the secrets-stripping case only.

Fixes, roughly in order of honesty:
1. **Validate before commit** — dry-build the graph (or at least resolve credentials for the
   target runtime) and refuse the save with the reason, changing nothing.
2. **Roll back the YAML** when the rebuild fails.
3. At minimum, say so plainly: the toast reads "config saved · graph rebuild failed", which
   understates "your config and your running agent now disagree".

Related IA consequence: the *reason* you can hit this by accident is D-B — the runtime field is
two sections from the key it makes mandatory. See the target doc's Decision B.

### D9 — Knowledge is a 25-field category carrying non-knowledge concerns (**low**)

Split into Recall (12) / Ingestion (7) / History (6) by `_KNOWLEDGE_SUBSECTION`. "History" is
`checkpoint.*` — conversation-history retention and pruning, which is chat-history plumbing
rather than knowledge. It is the largest category by 8 fields.

---

## 3. Full field inventory (101 fields, 8 categories)

`reachable via` is where an operator can actually change it today.

<!-- BEGIN GENERATED INVENTORY -->

### Category: `Behavior` — 17 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `agent_runtime` | Agent runtime | select | agent | — | Settings ▸ Behavior |
| `operator_mcp.tools` | Restrict tools for the ACP brain | string_list | agent | — | Settings ▸ Behavior |
| `compaction.enabled` | Enable compaction | bool | agent | — | Settings ▸ Behavior |
| `compaction.trigger` | Trigger | string | agent | — | Settings ▸ Behavior |
| `compaction.keep_messages` | Keep last N messages | number | agent | — | Settings ▸ Behavior |
| `compaction.model` | Summarizer model | string | agent | — | Settings ▸ Behavior |
| `goal.enabled` | Enable goal mode | bool | agent | — | — (ui_hidden: YAML only, by design) |
| `goal.max_iterations` | Max continuations | number | agent | — | Settings ▸ Behavior |
| `goal.eval_model` | Verifier model | string | agent | — | Settings ▸ Behavior |
| `middleware.knowledge` | Knowledge middleware | bool | agent | — | Settings ▸ Behavior |
| `middleware.memory` | Memory middleware | bool | agent | — | Settings ▸ Behavior |
| `middleware.audit` | Audit middleware | bool | agent | — | Settings ▸ Behavior |
| `middleware.scheduler` | Scheduler | bool | agent | — | Settings ▸ Behavior |
| `middleware.enforcement` | Tool enforcement | bool | agent | — | — (ui_hidden: YAML only, by design) |
| `background.auto_resume` | Push-resume on completion | bool | agent | — | Settings ▸ Behavior |
| `runtime.autostart_on_boot` | Autostart on boot | bool | agent | yes | Settings ▸ Behavior |
| `developer.channel` | Developer channel | select | agent | — | Settings ▸ Behavior |

### Category: `Box` — 12 fields

Schema-driven panel: **NO schema panel**

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `telemetry.fleet_trace_export` | Fleet trace export | bool | agent | yes | **NONE — unreachable** |
| `telemetry.enabled` | Store telemetry locally | bool | host | yes | Telemetry ▸ chip |
| `telemetry.retention_days` | Telemetry retention (days) | number | host | yes | Telemetry ▸ chip |
| `network.bind` | Bind interface | string | host | yes | Fleet ▸ Box runtime chip |
| `egress.allowed_hosts` | Outbound host allowlist | string_list | host | — | **NONE — unreachable** |
| `fleet.port_base` | Workspace port base | number | host | yes | Fleet ▸ Box runtime chip |
| `fleet.discovery.port_min` | Discovery scan: min port | number | host | — | Fleet ▸ Box runtime chip |
| `fleet.discovery.port_max` | Discovery scan: max port | number | host | — | Fleet ▸ Box runtime chip |
| `fleet.discovery.mdns` | mDNS discovery | bool | host | — | Fleet ▸ Box runtime chip |
| `fleet.warm.max` | Warm-agent cap | number | host | — | Fleet ▸ Box runtime chip |
| `fleet.warm.grace_seconds` | Warm eviction grace (s) | number | host | — | Fleet ▸ Box runtime chip |
| `fleet.autostart` | Autostart members | string_list | host | yes | **NONE — unreachable** |

### Category: `Capabilities` — 8 fields

Schema-driven panel: **NO schema panel**

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `skills.scope` | Skill sharing | select | agent | — | Skills ▸ Skill sharing chip |
| `commons.path` | Shared skills location | string | host | — | Skills ▸ Skill sharing chip |
| `mcp.scope` | MCP server sharing | select | agent | — | MCP ▸ sharing chip |
| `filesystem.enabled` | Filesystem tools | bool | agent | — | Tools ▸ Shell & filesystem chip |
| `filesystem.allow_run` | Allow run_command <br>*depends_on: `filesystem.enabled`* | bool | agent | — | Tools ▸ Shell & filesystem chip |
| `filesystem.run_requires_approval` | Require approval per command <br>*depends_on: `filesystem.allow_run`* | bool | agent | — | Tools ▸ Shell & filesystem chip |
| `filesystem.bypass_allowed` | Allow /bypass <br>*depends_on: `filesystem.run_requires_approval`* | bool | agent | — | Tools ▸ Shell & filesystem chip |
| `tools.disabled` | Disabled tools | string_list | agent | — | Tools ▸ per-row switches |

### Category: `Identity` — 6 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `identity.name` | Agent name | string | agent | — | — (ui_hidden: YAML only, by design) |
| `identity.operator` | Operator | string | agent | — | Settings ▸ Identity |
| `identity.org` | Organization | string | host | — | Settings ▸ Identity |
| `operator.project_dir` | Project directory | string | agent | — | Settings ▸ Identity |
| `operator.allowed_dirs` | Allowed project dirs | string_list | agent | — | Settings ▸ Identity |
| `auth.token` | A2A auth token | secret | agent | — | Settings ▸ Identity |

### Category: `Knowledge` — 25 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `knowledge.top_k` | Knowledge recall top-k | number | agent | — | Settings ▸ Knowledge + Knowledge store ▸ Recall chip |
| `knowledge.inject_namespaces` | Auto-inject namespaces | string_list | agent | — | Settings ▸ Knowledge |
| `knowledge.inject_min_trust` | Auto-inject trust floor | number | agent | — | Settings ▸ Knowledge |
| `knowledge.hot_write_confirm` | Confirm agent hot-memory writes | bool | agent | — | Settings ▸ Knowledge |
| `knowledge.scope` | Knowledge sharing | select | agent | yes | Settings ▸ Knowledge |
| `knowledge.embeddings` | Semantic recall (embeddings) | bool | agent | yes | Settings ▸ Knowledge + Knowledge store ▸ Recall chip |
| `knowledge.embed_model` | Embedding model | select | agent | — | Settings ▸ Knowledge |
| `knowledge.recall_preview_chars` | Recall preview length | number | agent | yes | Settings ▸ Knowledge |
| `knowledge.vector_k` | Hybrid candidate pool | number | agent | yes | Settings ▸ Knowledge |
| `knowledge.rrf_k` | RRF constant (k) | number | agent | yes | Settings ▸ Knowledge |
| `knowledge.min_score` | Recall relevance floor | number | agent | yes | Settings ▸ Knowledge |
| `skills.top_k` | Skills listed in context | number | agent | — | Settings ▸ Knowledge |
| `knowledge.transcribe_model` | Transcription model | string | agent | yes | Settings ▸ Knowledge |
| `knowledge.image_describe_model` | Image description model | string | agent | yes | Settings ▸ Knowledge |
| `knowledge.chunk_max_chars` | Ingest chunk size | number | agent | yes | Settings ▸ Knowledge |
| `knowledge.chunk_overlap_chars` | Ingest chunk overlap | number | agent | yes | Settings ▸ Knowledge |
| `knowledge.contextual_enrichment` | Contextual enrichment | bool | agent | yes | Settings ▸ Knowledge |
| `knowledge.attach_inline_budget` | Chat attachment inline budget | number | agent | — | Settings ▸ Knowledge |
| `knowledge.facts` | Extract semantic facts | bool | agent | — | Settings ▸ Knowledge |
| `checkpoint.db_path` | Conversation history DB | string | agent | yes | Settings ▸ Knowledge |
| `checkpoint.keep_per_thread` | History: keep N per session | number | agent | — | Settings ▸ Knowledge |
| `checkpoint.max_age_days` | History: max age (days) | number | agent | — | Settings ▸ Knowledge |
| `checkpoint.prune_interval_hours` | History: prune every (hours) | number | agent | yes | Settings ▸ Knowledge |
| `checkpoint.harvest_enabled` | History: harvest to knowledge | bool | agent | — | Settings ▸ Knowledge |
| `checkpoint.vacuum` | History: reclaim disk after prune | bool | agent | — | Settings ▸ Knowledge |

### Category: `Model` — 17 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `model.name` | Primary model | select | host | — | Settings ▸ Model |
| `model.provider` | Provider | string | host | — | Settings ▸ Model |
| `model.api_base` | API base URL | string | host | — | Settings ▸ Model |
| `model.api_key` | API key | secret | agent | — | Settings ▸ Model |
| `model.temperature` | Temperature | number | agent | — | Settings ▸ Model |
| `model.max_tokens` | Max output tokens | number | agent | — | Settings ▸ Model |
| `model.thinking` | Thinking mode | select | agent | — | Settings ▸ Model |
| `model.reasoning_effort` | Reasoning effort | select | agent | — | Settings ▸ Model |
| `model.vision` | Vision (native images) | bool | agent | yes | Settings ▸ Model |
| `model.max_iterations` | Max tool iterations | number | agent | — | Settings ▸ Model |
| `model.favorites` | Favorites | string_list | agent | — | Settings ▸ Model |
| `routing.aux_model` | Auxiliary (fast) model | string | host | — | Settings ▸ Model |
| `routing.fallback_models` | Fallback models | string_list | host | — | Settings ▸ Model |
| `prompt_cache.enabled` | Enable prefix caching | bool | host | — | Settings ▸ Model |
| `prompt_cache.ttl` | Cache TTL | select | host | — | Settings ▸ Model |
| `prompt_cache.warm.enabled` | Cache warming | bool | host | — | Settings ▸ Model |
| `prompt_cache.warm.interval_seconds` | Warm interval (s) | number | host | — | Settings ▸ Model |

### Category: `Plugins` — 3 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `soul.self_edit_enabled` | Let the agent edit its own persona (SOUL.md) | bool | agent | — | — (ui_hidden: YAML only, by design) |
| `media.public` | Serve media without auth | bool | agent | — | Settings ▸ Plugins |
| `media.retention_days` | Media retention (days) | number | agent | — | Settings ▸ Plugins |

### Category: `Secrets` — 13 fields

Schema-driven panel: YES — `SettingsCategoryPanel`

| key | label | type | scope | restart | reachable via |
|---|---|---|---|---|---|
| `secrets_manager.enabled` | Pull secrets from a manager | bool | agent | — | Settings ▸ Secrets |
| `secrets_manager.provider` | Provider <br>*depends_on: `secrets_manager.enabled`* | select | agent | — | Settings ▸ Secrets |
| `secrets_manager.host` | Server URL <br>*depends_on: `secrets_manager.enabled`* | string | agent | — | Settings ▸ Secrets |
| `secrets_manager.project_id` | Project ID <br>*depends_on: `secrets_manager.enabled`* | string | agent | — | Settings ▸ Secrets |
| `secrets_manager.environment` | Environment <br>*depends_on: `secrets_manager.enabled`* | string | agent | — | Settings ▸ Secrets |
| `secrets_manager.path` | Secret path <br>*depends_on: `secrets_manager.enabled`* | string | agent | — | Settings ▸ Secrets |
| `secrets_manager.recursive` | Include subfolders <br>*depends_on: `secrets_manager.enabled`* | bool | agent | — | Settings ▸ Secrets |
| `secrets_manager.client_id` | Machine identity client ID <br>*depends_on: `secrets_manager.enabled`* | secret | agent | — | Settings ▸ Secrets |
| `secrets_manager.client_secret` | Machine identity client secret <br>*depends_on: `secrets_manager.enabled`* | secret | agent | — | Settings ▸ Secrets |
| `secrets_manager.refresh_seconds` | Refresh interval (seconds) <br>*depends_on: `secrets_manager.enabled`* | number | agent | — | Settings ▸ Secrets |
| `secrets_manager.required` | Required at boot <br>*depends_on: `secrets_manager.enabled`* | bool | agent | — | Settings ▸ Secrets |
| `secrets_manager.override_env` | Manager beats existing env <br>*depends_on: `secrets_manager.enabled`* | bool | agent | — | Settings ▸ Secrets |
| `secrets_manager.timeout_seconds` | Fetch timeout (seconds) <br>*depends_on: `secrets_manager.enabled`* | number | agent | — | Settings ▸ Secrets |

<!-- END GENERATED INVENTORY -->

---

## 4. Counts, for checking drift

| metric | value |
|---|---|
| total fields | 101 |
| categories | 8 |
| categories with a schema panel | 6 (Identity, Model, Behavior, Knowledge, Secrets, Plugins) |
| categories with **no** schema panel | 2 (Capabilities, Box) — 20 fields |
| deliberately hidden (`ui_hidden`) | 4 |
| **unreachable by accident** | **3** |
| reachable only via a chip | 17 |
| reachable via chip **and** a canonical panel (a true shortcut) | 2 |
| sections unmapped in `_SECTION_CATEGORY` (→ silently "Plugins") | 2 (Media, Persona) |

### Per category

| category | fields | schema panel? |
|---|---|---|
| Knowledge | 25 | yes |
| Behavior | 17 | yes |
| Model | 17 | yes |
| Secrets | 13 | yes |
| Box | 12 | **no** |
| Capabilities | 8 | **no** |
| Identity | 6 | yes |
| Plugins | 3 | yes |

---

## 5. Open questions for the target doc

Carried into [settings-ia-target.md](./settings-ia-target.md) — recorded here only so the
inventory captures what the rework has to answer.

1. Do Capabilities and Box get real schema sections (the Identity precedent), or does the chip
   become a legitimate, documented home?
2. Does the build-time vs denylist tool control (D2) collapse into one operator-facing concept?
3. Where does `agent_runtime` belong — Behavior or Model? (The console already merges runtime
   and model in the composer picker, #1993; ADR 0033 D1 holds they are separate axes.)
4. Where does `runtime.autostart_on_boot` belong once the one-field "Runtime" section goes?
5. Should `_category_for()` keep defaulting to "Plugins", or fail loudly on an unmapped section?
6. Do `checkpoint.*` stay in Knowledge?
7. Do the split domains (D8) get reunited?
