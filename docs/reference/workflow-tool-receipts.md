# Workflow tool receipts

With the workflows plugin enabled, inspect the structured result of a workflow
run/resume or `GET /api/plugins/workflows/runs/{run_id}`. Text step outputs,
template threading and the `run_workflow` tool's text return are unchanged.

## Contract (schema version 1)

`tool_receipts` has `schema_version: 1`, `source: native_audit`, an
`execution_id` identifying this physical execution segment, and a `steps` map.
Each step has `run_id`, `step_id`, `execution_id`, `capture_status`,
`capture_supported`, bounded `entries`, `dropped_count`, `partial_count`,
`pending_count` and `truncated_fields_count` (the sum of entry `dropped_fields`,
including omissions). Observed snapshots also include SDK `limits`, `closed`
and `invocation_id`.

| Capture status | Meaning |
| --- | --- |
| `unsupported` | Native capture was not acknowledged; absence of entries proves nothing. Old stored runs read this way. |
| `captured` | Native middleware participated and capture completed without known loss. Zero entries means no observed handler calls. |
| `partial` | Supported capture was interrupted or lost/omitted data. Inspect counts and individual entries. |
| `not_executed` | This segment did not dispatch the step: seeded, gated, rejected or otherwise pending. It was not reviewed with zero calls. |

`capture_supported` means native participation was observed in this scope, not a
promise that every external tool/runtime supports capture. During execution a
snapshot can be open and unsupported; crash recovery cannot establish completion.

Entries retain the SDK's tool `name`, redacted `args`, `tool_call_id`, unique
native handler `execution_id`, `invocation_id`, start `sequence`, `duration_ms`,
`result_state`, bounded `result_metadata`, `partial` and `dropped_fields`.
Workflow association adds `run_id`, `step_id` and `workflow_execution_id`.
Native IDs may repeat; native execution IDs distinguish physical handler calls.
Result states include `success`, `tool_error`, `exception`, `cancelled`, and
`unknown`. A successful empty read remains a successful observed call. Text in a
reply claiming to have used a tool creates no evidence.

For reviewer compatibility, `result.tool_calls[step_id]` contains detached safe
native entry dictionaries from the same snapshot, including `name` and `args`.
Always consult `tool_receipts.steps[step_id]` for capability/status and loss
counts before interpreting an empty list. The same fields are persisted on run
records, including paused and cancelled runs.

## Resume and retention

Each resume gets a new execution ID. Previously completed outputs are seeded;
the current segment marks those steps `not_executed` and exposes no new calls for
them. Earlier evidence remains under `tool_receipts.previous_executions`, with
its original execution identity. At most eight previous segments are retained;
`dropped_execution_count` counts evicted segments. Each segment is bounded by
the recipe's step count and the SDK's per-step limits. Historical records without
receipts remain unsupported, including when archived during resume.

The SDK caps each step at 128 entries, 131072 aggregate entry bytes, 8192 bytes
per entry, depth 6, 32 fields per container, 256 visited value nodes per entry,
and 512 bytes per string. Workflow provenance and envelope fields add overhead.
Oversized entries are dropped. Redaction precedes truncation; credentials,
file bodies, multipart payloads and unknown objects are omitted. Tool result
text is never copied as receipt evidence. Structured result leaves are limited
to recognized metadata such as paths, statuses, counts, sizes and revisions.
The compatibility map duplicates only this safe evidence. Existing workflow
text outputs retain their existing storage behavior.

Receipts share the workflow run JSON file's lifetime and access controls. The
existing terminal-run retention defaults to 200 records (`workflow_runs.max_runs`);
paused/running records are retained. No separate telemetry log or evidence store
is created. Writes remain best effort: a disk failure can leave older evidence.

Receipts prove observed native handler execution, not semantic review coverage,
correct repository selection, context isolation, successful comprehension, or
review quality. Missing or wrong arguments remain visible as supplied; a tool
error remains an observed attempt. Policy short-circuits are not handler calls.
Consumers must evaluate the arguments and result state against their own review
requirements and must not infer complete coverage from `captured` alone.

## Review context (schema version 1)

`review_context` accompanies `tool_receipts` in structured results and persisted
readback. Its `steps` map contains a versioned envelope for each step with
`run_id`, `step_id`, and the receipts' segment `execution_id`. `status` is
`observed` only when the native subagent has been constructed and is about to be
invoked; `unknown` means dispatch occurred without construction evidence, and
`not_executed` identifies seeded, pending, gated or rejected steps. Legacy records
return `{"schema_version": 1, "status": "unknown"}`.

An observed envelope records:

- `conversation: fresh` and `parent_history: not_supplied`: this native runner
  constructs a new graph and supplies only one user message. A shared tracing or
  ledger session does not imply shared conversation history.
- `inputs.prompt`: the rendered prompt actually supplied, including prior findings
  or history interpolated by the recipe. `inputs.history: not_supplied` means no
  separate message history was supplied; it does **not** mean the prompt contains
  no prior findings. The engine cannot infer the provenance of arbitrary text.
- `instruction_sources`: the registered subagent name and assembled system prompt.
  External instruction sources and later runtime additions remain `unknown`.
- `tool_restrictions`: bound tool names, the effective inherited fence, and
  enforcement configuration. An empty fence means no additional inherited fence;
  bound tools still constrain availability. External policy remains `unknown`.

`workflow_state` is independently `fresh`, `resumed`, or `seeded`. A step in a
resumed workflow still has a **fresh conversation** when constructed without
parent messages. Seeded steps get no new construction evidence: `origin` references
the original `run_id`, `step_id`, and `execution_id` when available in the run's
history. Bare externally supplied `seed_outputs` strings carry no source identity,
so their origin is `unknown`. Python workflow callers may also supply
`seed_run_id` to identify a source record in the same runs store. The engine links
only steps whose stored output agrees with the supplied seed; a missing source,
legacy source without evidence, or mismatched output leaves origin unknown.
Matching text alone is never used to discover an origin.
Previous envelopes retain their original identities under `previous_executions`,
with the same eight-segment retention and `dropped_execution_count` convention as
receipts. A reference can outlive its retained historical envelope.

Prompt and system-prompt text is credential-redacted and limited to 16,384
characters each; `characters` counts redacted text and `omitted_characters`
reports truncation. These fields can contain review inputs and share the run
record's access controls and retention. Construction evidence is persisted before
invocation, including for subsequently failed or cancelled steps. It establishes
what the runner supplied, not model comprehension, later context changes, or
complete review coverage. Consumers must still inspect receipt capability,
status and loss counters.
