# Export or copy an agent

A snapshot is a zip containing an agent's setup: persona, configuration, plugin
pins, MCP definitions, and skills. Importing creates a **new agent**. To recover
existing chats, credentials, or runtime state, use a
[data backup](/guides/backup-and-restore).

## What a snapshot includes {#what-travels-and-what-doesn-t}

| Included | Supplied separately on the destination |
| --- | --- |
| Persona and configuration, with recognized secrets removed | Credentials and subscription sign-in |
| Plugin repository URLs and pinned revisions | Plugin code, installed during import |
| MCP definitions with secret values removed | Required commands and their credentials |
| Skill directories | Managed runtimes and local work folders |
| Optional knowledge text, with the CLI export flag | Chat history and runtime databases |

Read the review and exported files before sharing. The exporter strips known
secret fields and recognizable credential patterns; it cannot recognize every
private detail or secret embedded in prose.

## Export from the app {#export}

1. Open **Settings → Snapshot** for the agent you want to copy.
2. Read the review: credentials the destination needs, scrubbed text, and paths
   to change on the destination.
3. Choose **Download snapshot** and keep the zip. It includes the review as
   `REVIEW.md`. If the agent changed during review, the app refreshes it;
   read the refreshed review before downloading.

<span id="two-kinds-of-finding-two-different-responses"></span>

A credential scrubbed from the export remains in the source agent. Replace or
remove an exposed credential there too. A scrubbed machine-local path needs to
be selected again on the destination.

## Import into a new agent {#import}

1. Open **Settings → Fleet → New agent → From a snapshot**.
2. Choose the zip and give the new agent a name.
3. Review the plugin repositories, granted capabilities, and required credentials.
   Import installs and runs the plugin code named in the plan.
4. Supply the required credentials you want this agent to use, then choose the
   button that installs the listed plugins and creates the agent.
5. Open the new fleet member and send a short test message. If it needs setup,
   follow the setup prompt or [connect a model](/guides/model-connections).
6. Check its work folders and any local commands. A path or binary from the
   source machine may not exist on this one. Install the
   [document runtime](/guides/python-runtime) if the destination desktop needs it.

<span id="importing-runs-code-—-read-the-plan"></span>
<span id="the-new-agent-arrives-incomplete"></span>

Only import a snapshot whose plugin sources you trust. Its filesystem, shell,
MCP, delegate, and tracing settings travel with the definition and are shown
in the plan. Review them before applying it.

## Duplicate on the same machine {#duplicating-an-agent}

Export the source agent and import it under another name using the steps above.
The duplicate has its own fresh history and stores. Re-enter credentials as
needed, even when both agents live on the same machine.

## Include knowledge {#carrying-knowledge-opt-in}

The app's export is definition-only. The CLI can also include private knowledge:

```bash
protoagent agent export --include-knowledge
```

Review this zip as carefully as the source documents. It can contain learned
facts, project details, names, and credentials embedded in knowledge text.
Session-summary files, the designated memory domains, and always-on entries
are excluded; **that does not mean every personal fact is excluded**.

The destination ingests this text into its own store for keyword recall. It
must compute embeddings using its own gateway for semantic recall; copies of
seed files stay in `knowledge-seed/` for that purpose.

<span id="carrying-knowledge-opt-in-1"></span>
<span id="the-model-connection-travels-as-a-registry"></span>

For CLI export/import commands, exact exclusions, connection inheritance, and
older snapshot compatibility, see [Snapshot reference](/reference/agent-snapshots).
