# Background job sessions — pre-release Work verification

Acceptance evidence for background job sessions ([ADR 0129](../adr/0129-work-consolidation.md)
D6, #5110) and the short Work checklist to run before the next release. Running this list
verifies the feature; it does **not** authorize a release.

## Automated evidence

Run from the repo root (first two are also part of the normal CI gate):

| Command | Proves |
|---|---|
| `python -m pytest tests/test_background_job_isolation.py -q` | S4 acceptance: a job parked on a real decision interrupt plus a long multi-step turn never delay an unrelated scheduled fire or inbox now-item past the documented dispatch bound (one scheduler poll tick; the test allows 40 scaled ticks for CI noise); legacy NULL/`system:activity` stores migrate idempotently and restart to exactly one invocation per intended fire across queued/accepted/orphaned occurrence states; a parked job is never superseded; background waits stay isolated per execution session; a job-session decision is listed and answered fleet-wide over the real `/api/decisions` routes and resumes only its owning job. |
| `python -m pytest tests/test_scheduler_local.py tests/test_job_session_routing.py tests/test_job_session_lifecycle.py tests/test_decision_needed.py -q` | The S1–S3 foundations: durable execution identity + occurrence ledger, producer routing to job sessions, job-scoped context/retirement, and the decision park/answer contract. |
| From `apps/web`: `npm run build --workspace @protoagent/web` once, then `PROTOAGENT_E2E_PYTHON=../../.venv/bin/python npx playwright test e2e/background-job-decisions-live.spec.ts` | The REAL backend end to end (`python -m server` on an isolated instance/box root, model faked at the gateway): two inbox-fired jobs park on real decision interrupts in their own `job:` sessions; the Work panel lists both fleet-wide, the Needs-you pill stays visible from another chat, answering one from Work resumes only its owning job, and every informative state is neutral copy with no error banner. CI's web-e2e shards skip this spec (no Python runtime); run it locally. |

## Manual Work checklist (~5 minutes, dev instance)

Use the sandbox instance (`scripts/dev.sh`, console on `:7871`) so nothing touches real chats.

1. **Seed a background decision.** Either ask the agent in a chat: *"Schedule a job one
   minute from now that uses request_decision to ask me whether to proceed"* — or seed
   without a model turn: `curl -X POST localhost:7871/api/inbox -H 'Content-Type: application/json'
   -d '{"text": "Use request_decision to ask me whether to ship.", "priority": "now", "source": "verify"}'`.
2. **Fleet-wide marker.** When the job parks, the Decisions pill reads "Decisions, 1
   waiting on you" in the current chat. Open a **new** chat: the pill (the approved
   Needs-you surface) is still there, unchanged layout.
3. **Answer from Work, never opening the session.** Click the pill → Needs you lists the
   decision with neutral meta ("Asked …" — no session UUIDs, no error banner). Open it and
   answer. Expect `Sent: "<option>".` and the pill count to drop. Do not open the job's
   session at any point.
4. **Attribution.** The Activity feed shows the job's completion attributed to the job
   (its trigger/origin), not as a resumed chat. If the job was scheduled from a chat, that
   chat gets its usual report card; the decision was still answerable without it.
5. **Restart safety (optional, deeper).** Park a second decision, restart the server, and
   confirm the decision is still pending and answerable, the job fires no duplicate turn,
   and nothing in Work shows an error state — only neutral copy.

If any step fails, file it against #5110 with the step number; the automated suites above
localize the layer (scheduler/ledger vs. routes vs. console).
