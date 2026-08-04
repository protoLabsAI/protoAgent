# Agent snapshots — export, share, duplicate

A **snapshot** is an agent's *recipe*: its persona, config, plugin pins, MCP servers and
skills, as a small zip. It is not a backup — no conversation history, no credentials, no
plugin code. Importing one yields a **fresh** agent, not a resumed one.

Governed by [ADR 0091](../adr/0091-agent-snapshot-portability.md).

## What travels, and what doesn't

| Travels | Doesn't |
|---|---|
| `SOUL.md` (the persona) | Conversation history, checkpoints, telemetry |
| Config, secret-stripped | `secrets.yaml`, `.fleet-token`, device tokens |
| `plugins.lock` pins (url + resolved SHA) | Plugin **code** — re-installed from the pins |
| MCP server definitions (values nulled) | **Memory** — never, under any flag |
| Knowledge — **only if you opt in** (see below) | |
| `SKILL.md` directories | Every runtime sqlite store |

The bar is 12-Factor's: **the artifact could be pushed to a public gist without leaking a
credential.** There is a test that greps a built snapshot's bytes for known secrets.

That holds for a **definition-only** snapshot, which is the default. It does **not** hold
once you add a knowledge seed — see [Carrying knowledge](#carrying-knowledge-opt-in).

## Export

**Console** — Settings ▸ Agent ▸ Snapshot. It opens on a *review*: which credentials the
target will need, what was scrubbed, what was skipped. The download is a second click.

**CLI** — works on a **stopped** agent, which is the usual case when you're moving one:

```bash
protoagent agent export --dry-run          # the review; writes nothing
protoagent agent export -o ~/snapshots/
```

Every zip carries a `REVIEW.md` describing what was stripped, so the disclosure can't get
separated from the artifact.

### Two kinds of finding, two different responses

The exporter runs a pattern sweep over free text (a token pasted into `SOUL.md` or a config
field isn't key-shaped, so the structural strip can't see it). What it reports splits in two:

- **Credential-shaped text** — scrubbed from the snapshot, but **still in the source agent**.
  Treat it as exposed: rotate it, then remove it there.
- **Machine-local paths** — scrubbed because they carry your username. Nothing to rotate;
  re-point them on the target.

Scrubbing free text is a **safety net, not a guarantee**. It can't recognize a credential
that reads like ordinary prose, so read the artifact before publishing it.

## Carrying knowledge (opt-in)

```bash
protoagent agent export --include-knowledge
```

Off by default. Turned on, the agent's knowledge travels as domain-tagged markdown under
`knowledge/` — text, not the raw sqlite, because the source's embeddings were computed
against *its* gateway and mean nothing on a target that may use a different model.

**This changes what the file is.** A definition-only snapshot is publishable; one carrying
knowledge is not. The two risks sit on different axes:

- **Credentials** — the export strips them, and a test asserts none survive.
- **Knowledge** — contains no credentials and may still be the last thing you want public:
  project detail, client names, internal notes.

So the review retracts the publishable claim, at the top and in bold, and lists every domain
with its chunk count so you can decide domain by domain. Treat the file the way you'd treat
the source documents themselves.

**Memory is never included** — not even with the flag. What an agent recalls about a
person's sessions ([ADR 0069](../adr/0069-memory-delivery-layer.md)) is a different kind of data
with a different consent question, and a snapshot is something you hand to someone else.
Knowledge can be reviewed a domain at a time; accreted personal memory realistically cannot.

On import the seed is re-ingested into the new agent's own store, so it is **searchable
immediately**. Semantic recall needs embeddings the target must compute itself, and its
gateway may not be configured yet — so the source docs are also kept at `knowledge-seed/` in
the new workspace. Run `protoagent knowledge ingest` on them once the gateway is set up.

## Import

**Console** — Settings ▸ Fleet ▸ New agent ▸ *From a snapshot*.

**CLI**:

```bash
protoagent agent import vera-snapshot.zip --dry-run
protoagent agent import vera-snapshot.zip --name vera-2 --yes \
  --secret model.api_key=sk-…
```

### Carrying knowledge (opt-in)

```bash
protoagent agent export --include-knowledge
```

Off by default. Turned on, the agent's knowledge travels as domain-tagged markdown under
`knowledge/` — text, not the raw sqlite, because the source's embeddings were computed
against *its* gateway and mean nothing on a target that may use a different model.

**This changes what the file is.** A definition-only snapshot is publishable; one carrying
knowledge is not. The two risks sit on different axes:

- **Credentials** — the export strips them, and a test asserts none survive.
- **Knowledge** — contains no credentials and may still be the last thing you want public:
  project detail, client names, internal notes.

So the review retracts the publishable claim, at the top and in bold, and lists every domain
with its chunk count so you can decide domain by domain. Treat the file the way you'd treat
the source documents themselves.

**Memory is never included** — not even with the flag. What an agent recalls about a
person's sessions ([ADR 0069](../adr/0069-memory-delivery-layer.md)) is a different kind of data
with a different consent question, and a snapshot is something you hand to someone else.
Knowledge can be reviewed a domain at a time; accreted personal memory realistically cannot.

On import the seed is re-ingested into the new agent's own store, so it is **searchable
immediately**. Semantic recall needs embeddings the target must compute itself, and its
gateway may not be configured yet — so the source docs are also kept at `knowledge-seed/` in
the new workspace. Run `protoagent knowledge ingest` on them once the gateway is set up.

## Importing runs code — read the plan

A snapshot names plugin repositories. Applying it clones them and enables their code
**in-process, with your privileges**. So import is always two steps: you get a *plan* first
— every plugin URL (with unfamiliar sources flagged), every capability the config grants,
every credential needed — and nothing is written until you accept it. The CLI refuses
without `--yes`; the console's button says what it is about to install.

Capability settings (`filesystem.allow_run`, `operator.allowed_dirs`, `mcp.servers`,
`delegates`) apply **verbatim** and are shown in the plan rather than stripped. They're part
of the agent's definition — silently neutering them would hand you a duplicate that behaves
differently for reasons the plan couldn't enumerate. protoAgent's model is trust and consent,
not sandboxing ([ADR 0071](../adr/0071-plugin-permissions-trust-model.md)): the control is
seeing what you're accepting.

Import only snapshots from a source you trust, exactly as you would a plugin or a
dependency.

### The new agent arrives incomplete

No credentials travel, so a freshly imported agent can't reach its gateway until you supply
them — via `--secret NAME=VALUE`, the console's import form, or that agent's Settings ▸
Secrets afterwards. Only credentials the **source** agent actually had are reported missing;
one a plugin merely declares isn't, because the original didn't have it either.

## Duplicating an agent

Export from the source, import with a new name:

```bash
protoagent agent export -o /tmp/vera.zip
protoagent agent import /tmp/vera.zip --name vera-staging --yes
```

Identity is re-stamped on import — the copy gets its own name, its own instance id, and its
own data scope, so the two never collide.
