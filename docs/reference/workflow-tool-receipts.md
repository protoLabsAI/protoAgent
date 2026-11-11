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
