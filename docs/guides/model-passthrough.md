# Call a model directly

Model passthrough makes a pure model call through any configured connection:
a gateway, a local endpoint, or a native subscription such as `anthropic-oauth`
or `openai-codex`. It is **not the agent's `/v1/chat/completions`**, which runs
the agent graph. Passthrough runs no graph, executes no tools, and adds no SOUL
or memory.

## Make a call

You need a running protoAgent instance, a working [model connection](/guides/model-connections),
its connection ID and model ID, and the instance's operator bearer token when
authentication is configured. Subscription connections must already be signed in.

Send a non-streaming request to:

```text
POST /api/models/{connection}/v1/chat/completions
```

Use the connection's **Id**, not its display name or provider type. Put the bare
model ID in `model`; do not prefix it with the connection ID. Replace the port
and model below with those of your instance and connection:

```bash
curl 'http://127.0.0.1:7870/api/models/anthropic-oauth/v1/chat/completions' \
  -H "Authorization: Bearer ${A2A_AUTH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "required",
    "stream": false
  }'
```

Expect an OpenAI-shaped `chat.completion` response. For example, a model that
selects the supplied tool can return the following (IDs, timestamps, arguments,
and token counts vary):

```json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "created": 1793232000,
  "model": "claude-sonnet-4-6",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_example",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
}
```

`tools` supplies schemas to the model; returned `tool_calls` describe its choices.
The caller decides whether to execute them. Function `arguments` is a JSON
string. When there are no tool calls, `tool_calls` is omitted and `finish_reason`
is `stop`. Assistant content is returned as text.

## Supported request fields

- `model` must be nonempty; it does not default to the agent's primary model.
- `messages` accepts `system`, `user`, `assistant` (including prior `tool_calls`),
  and `tool` messages with `tool_call_id`. Supply any conversation history yourself.
- `tools` and `tool_choice` use OpenAI shapes. For Anthropic subscription
  connections, choices such as `required` and a named function are translated
  to the provider's native form.
- `temperature` and `max_tokens` are forwarded where supported. Anthropic OAuth
  drops `temperature`; Codex drops both fields and uses model defaults.
- `stream` must be `false` or omitted. Streaming is not supported.

Other fields, including `top_p` and `n`, are ignored. This is a chat-completions
adapter, not a complete implementation of every OpenAI endpoint or option.
Unknown connections return 404; missing models or `stream: true` return 400;
model invocation failures return 502.

## Authentication and remote access

Use `Authorization: Bearer <operator-token>` with the instance's `auth.token`
or `A2A_AUTH_TOKEN` value, not the upstream provider's API key. The route uses
the normal `/api` authentication middleware; an instance configured in open
mode does not require a bearer token.

Passthrough is additionally **loopback-only by default**: the TCP peer must be
loopback (`127.0.0.0/8`, `::1`, or `localhost`), otherwise the route returns 403.
This checks the peer seen by the server, not a forwarded client-address header;
a local reverse proxy can therefore appear as a loopback caller.

To permit remote callers, enable **Settings → Model → Model passthrough → Allow
remote callers** and **Save & apply**, or set:

```yaml
model_passthrough:
  allow_remote: true
```

The default is `false`. The route reads the active setting on every request.
Enabling it removes only the loopback restriction; bearer authentication still
applies when configured. See [Expose to the world](/guides/exposing-protoagent)
for deployment and access setup.

## Usage accounting

Calls appear in the console's `/usage` view (backed by `/api/usage`) through the
same telemetry store as chat. Records identify the connection with
`session_id: model_passthrough:<connection>` and a `model_passthrough:` task ID.
Native subscription calls carry their provider and account attribution; gateway
connections have empty provider/account fields. Response `usage` contains
provider-reported token counts, or zeros if none are reported. Telemetry writes
are best-effort and do not fail an otherwise successful model call.

## Evaluate tool selection

For an OpenAI-compatible eval client, set its base URL to the connection's
`/v1` prefix, so it appends `/chat/completions`. For example, from the
**careercoach checkout**, which owns `evals/tool_selection/run_eval.py`:

```bash
EVAL_BASE_URL=http://127.0.0.1:<port>/api/models/anthropic-oauth/v1 \
EVAL_MODEL=claude-sonnet-… python evals/tool_selection/run_eval.py
```

Replace `<port>` and `claude-sonnet-…` before running, and configure the eval
client to send your protoAgent operator bearer token. That script is not bundled
in this repository. This evaluates the model's tool selection without executing
the agent's tools. To evaluate full agent behavior instead, use
[Eval your fork](/guides/evals).
