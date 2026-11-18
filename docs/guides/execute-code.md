# Let the agent run code and call tools from scripts

`execute_code` gives the agent a Python interpreter. Its main use is calling
several tools from one script: the agent fetches a large result, filters it in
code, and reads only the answer. That takes fewer model round-trips than calling
each tool in turn, and the intermediate results stay out of the conversation.

## Before you start

- `execute_code` is **on by default**, because the bundled Cowork pack uses it
  to create documents. To turn it off, add it to `plugins.disabled`.
- On the desktop app, install the [Python runtime](/guides/python-runtime) first.
- It runs code the model wrote, as the user the server runs as. A scrubbed
  environment and a timeout are isolation, not a sandbox. Read
  [Sandboxing & egress](/guides/sandboxing) before you enable it for anything
  that handles untrusted input.

## Try it

Ask for a job that needs many reads and a small answer:

> Using execute_code, read every Python file under `src/` in this project and
> tell me the five longest, with line counts.

The chat shows one `execute_code` card. The tool calls the script made appear
as cards nested inside it. The agent's reply contains the answer, not the file
contents.

## Choose which tools scripts can call

A script reaches tools through a `tools` object, for example
`tools.read_file(project="app", path="README.md")`. By default it can call only
these read tools:

`read_file`, `list_dir`, `find_files`, `search_files`, `fetch_url`,
`web_search`, `memory_search`, `knowledge_search`, `memory_list`, `current_time`

To add more, open **Settings**, find the **Execute Code** group, and add tool
names to **Additional exposed tools**. Or set it in the config file:

```yaml
execute_code:
  extra_tools:
    - github_list_issues
    - github_search_issues
    - board_list
```

| Setting | Config key | What it does |
| --- | --- | --- |
| Additional exposed tools | `extra_tools` | Adds tools to the set in use. Use this in most cases. |
| Exposed tools | `tools` | Empty means the default read set above. A non-empty list **replaces** the default with exactly the names you list. |

Guidance on what to add:

- **Add read tools that return a lot.** Issue lists, search results, file
  reads and board listings are where a script saves the most.
- **Add a write tool only for a specific scripted job.** A script can call a
  tool in a loop, so one approved `execute_code` call can write many times.
- **Some tools can never be added:** `ask_human`, `request_user_input`, `task`,
  `task_batch` and `execute_code` itself. A script cannot wait for a person or
  delegate to another agent.
- **Keep the total to about 25.** Up to 25 tools are listed in the tool's
  description with their full call signatures. Past that the rest are listed by
  name only, and the script looks a signature up with `tools.help("name")`.

A name that is not a registered tool is ignored, with a warning in the log.
Tool denylists and rate limits apply to a call from a script exactly as they do
to a direct call.

### Add tools from an MCP server

MCP tools are not in the default set. Add them by name, or add every tool from
one server with `mcp:<server>/*`:

```yaml
execute_code:
  extra_tools:
    - "mcp:archiver/*"
```

See [Connect MCP servers](/guides/mcp) to set a server up.

## Keep variables between calls (experimental)

By default each call runs in a new process and nothing carries over. Turn on
**Keep state across a session's runs** (`execute_code.session_state: true`) and
each chat gets its own long-lived Python process. Variables, imports and
functions from one call are available in the next, so the agent can load
something once and answer several questions from it.

What to expect:

- The first call in a chat starts with the line `new kernel: no earlier state`.
- A call that runs past the timeout stops the process. The result says
  `kernel restarted: earlier variables are gone`.
- The agent can pass `fresh=true` to start clean on purpose.
- State is per chat. It is never saved, so a server restart clears it.
- The process stops when the chat is deleted, cleared or closed, or after it
  has been idle for the idle timeout.

| Setting | Config key | Default | What it does |
| --- | --- | --- | --- |
| Keep state across a session's runs | `session_state` | `false` | One long-lived process per chat. |
| Session kernel idle timeout (s) | `kernel_idle_ttl` | `600` | An unused process is stopped after this many seconds. |
| Max concurrent session kernels | `max_kernels` | `4` | Starting one more stops the least recently used. |

## Limits

| Setting | Config key | Default | What it does |
| --- | --- | --- | --- |
| Script timeout (s) | `timeout` | `30` | The script is stopped after this long. |
| Output truncate (chars) | `output_truncate` | `6000` | Longer output is cut. The full text is saved to a scratch file and the result names its path. |

A script can also set a variable named `result` to return structured data as
JSON, as well as or instead of printing.

## Check what a script did

Every tool call a script makes is written to the
[audit log](/guides/observability#audit-log) with a `ptc:` prefix, tied to the
`execute_code` call that made it. The script itself is recorded too.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| The agent says it has no `execute_code` tool | Check that `execute_code` is not in `plugins.disabled`. On desktop, install the [Python runtime](/guides/python-runtime). |
| A script reports that a tool does not exist on `tools` | Add the tool to **Additional exposed tools**. Check the log for a warning that the name was ignored. |
| `Blocked by policy` in a script's output | The tool is denied or rate limited for this agent. The same call would be refused outside a script. |
| The second call cannot find a variable | Check that **Keep state across a session's runs** is on and that both calls are in the same chat. A timeout, the idle timeout or a restart also clears state. |
| Output ends with a marker naming a scratch file | The output passed the truncate limit. Ask the agent to read the rest from that file, or to print less. |

## Related

- [Configuration reference: `execute_code`](/reference/configuration#execute-code)
- [Sandboxing & egress](/guides/sandboxing)
- [ADR 0103](/adr/0103-programmatic-tool-calls-from-execute-code) and
  [ADR 0134](/adr/0134-execute-code-session-kernels-and-mcp-bridging) record why
  it works this way.
