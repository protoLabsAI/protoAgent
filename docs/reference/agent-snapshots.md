# Agent snapshot format and CLI

For app export and import, follow [Export or copy an agent](/guides/agent-snapshots).
Snapshots transfer definitions. Full recovery uses [data backups](/guides/backup-and-restore).

## Export and import commands

These commands use the current resolved instance roots and can read a stopped
agent. In a source checkout, replace `protoagent` with `uv run python -m server`.

```bash
protoagent agent export --dry-run
protoagent agent export -o ~/snapshots/
protoagent agent export --include-knowledge
protoagent agent import agent-snapshot.zip --dry-run
protoagent agent import agent-snapshot.zip --name agent-copy --yes
```

`-o` accepts a zip path or destination directory. The default is a timestamped
zip in the current directory. Import `--dry-run` shows the plan without creating
an agent. Applying with `--yes` acknowledges the plugin code and capabilities
in that plan.

Import accepts repeatable `--secret NAME=VALUE`. Prefer the app's credential
form when you want to avoid putting a secret in shell history. A model key is
named by its connection, such as `providers.gateway`. Credentials are written
to the new agent, not restored from the zip. Requirements distinguish keys
present on the source from credentials merely declared by a plugin.

## Definition and review

A snapshot includes `SOUL.md`, redacted configuration, plugin URL/revision pins,
MCP definitions, and skill directories. Plugin code is fetched on import, not
stored in the zip. `REVIEW.md` accompanies the definition and lists required
secrets, pattern redactions, paths, and notes.

Runtime SQLite databases, chat history, session-summary files, device tokens,
`secrets.yaml`, and fleet tokens do not travel. The redactor strips recognized
secret fields and patterns in free text, including persona text. Unrecognized
credentials or private prose can remain; a passing known-secret test does not
establish that an arbitrary snapshot is safe to publish.

Capability settings such as `filesystem.allow_run`, `operator.allowed_dirs`,
`mcp.servers`, `delegates`, and `tracing.host` retain their meaning and are shown
in the import plan. Imported plugin code runs in the host process with its
privileges. A snapshot is not a sandbox.

## Knowledge seed exclusions

`--include-knowledge` writes private-store chunks as domain-tagged Markdown.
It does not transfer source embeddings or raw SQLite. Commons-tier chunks are
excluded. So are domains `hot`, `session`, `conversation`, and `finding`, and
any entry with `delivery_policy="always"`, regardless of domain.

Other domains, including extracted `fact` entries, can travel. Facts learned
from conversation can contain personal or project information. Knowledge text
is not made private-information-free by the definition's secret-field scrub.
The collector caps selected chunk text at 2,000,000 characters and reads at most
10,000 chunks per domain. It can omit chunks without a truncation notice; check
the exported domain counts and files before relying on them.

Import re-ingests the seed into the destination's store and keeps its text at
`knowledge-seed/`. Keyword search can use it immediately; semantic indexing
requires the destination embedding gateway. Run `protoagent knowledge ingest`
on those files after configuring that gateway.

## Model connection inheritance

Exports use the provider-registry shape where the source's model settings can
be represented there. Native subscription models become qualified values,
such as `anthropic-oauth:<model>`. A source's explicit legacy endpoint becomes a
`gateway` connection when no box connections already define that lane. Values
inherited from the source box are omitted so the destination can inherit its
own box settings instead; a blank endpoint is not used to overwrite them.

The manifest records `model_aliases` for runtime paths that still read legacy
fields, and import restores the corresponding explicit values from their
connections. Older snapshots are staged into the same registry shape before
planning import. Keys required for migrated legacy routes are reported under
the connection they actually authenticate.

No subscription credential travels. A target whose model connection is missing
or unsigned-in stays incomplete until repaired. Use
[Connect and change models](/guides/model-connections) for that repair.

The design record is [ADR 0091](/adr/0091-agent-snapshot-portability).
