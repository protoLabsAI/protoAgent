- **Operator API status codes and validation changed for clients (#3973).** These are
  visible to anything calling `/api` directly — see "Error shapes" in the operator API
  reference.
  - `POST /api/chat` answers a failed turn with a real HTTP error instead of a `200`
    carrying the error as the reply (or a bare `500`): `429` / `502` / `503` / `400` /
    `500` on the `/v1` policy, a provider that closed the stream is now `502` on both
    surfaces, and the body is `{"detail": {code, message, upstream_status, session_id,
    error_id}}` (an object, not a string).
  - A missing id is `404`: `PUT` / `DELETE /api/scheduler/jobs/{id}` (were `400` /
    `200 {canceled: false}`) and task `PATCH`, close and `DELETE` (were `500` /
    `200 {deleted: false}`).
  - With no task store, the task routes answer `503 "tasks not enabled"`.
  - `POST /api/subagents/batch` takes at most 20 tasks, each with a non-empty `prompt`; a
    task without one now fails the whole batch with `422`.
  - `POST /api/goals` has a typed body: a wrong type, or `max_iterations` outside 1..1000
    (`no_progress_limit` 1..100), is a `422`; `max_iterations: 0` no longer means "the
    default".
  - `GET /api/background?status=` must be `running`, `completed`, `failed` or `canceled`,
    and every `/api/background/{id}` route rejects a malformed id with `400`.
  - A malformed `schedule` or `timezone` is refused at the scheduler route with `400`.
  - The console treats a `502` with a coded `detail` as the agent's own model failure,
    never as a cold start to retry, and the goal form caps `max_iterations` at 1000.
