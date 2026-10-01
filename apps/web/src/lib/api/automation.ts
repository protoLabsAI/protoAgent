/**
 * Autonomous work: background jobs (ADR 0050/0070), subagents, schedules, goals (ADR 0066/0079),
 * watches (ADR 0067), workflows (plugins/workflows) and the task board.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  BackgroundJobDTO,
  Task,
  GoalState,
  ScheduledJob,
  Subagent,
  ToolInfo,
  WatchState,
  WorkflowPausedRun,
  WorkflowRecipe,
  WorkflowRunRecord,
  WorkflowRunResult,
  WorkflowRunSummary,
  VerifierCatalog,
  WorkflowSummary,
} from "../types";
import type { WatchCreateBody } from "../../chat/watchForm";
import { request } from "./http";

export const automationApi = {
  // Background subagent jobs (ADR 0050) — the focused agent's registry. Read-only;
  // the UtilityBar pill + jobs dialog hydrate from this, then track live via the
  // `background.{started,completed}` bus events.
  background() {
    return request<{ enabled: boolean; jobs: BackgroundJobDTO[] }>("/api/background");
  },

  // One background job's full row by id (ADR 0070 D4). This is the ONLY place the
  // FULL result text is fetchable — the `background.completed` bus event and the
  // drained <task-notification> both carry truncated previews.
  backgroundJob(jobId: string) {
    return request<BackgroundJobDTO>(`/api/background/${encodeURIComponent(jobId)}`);
  },

  // Stop a running background job (ADR 0051) — cancels its detached A2A turn.
  stopBackground(jobId: string) {
    return request<{ ok: boolean; status?: string; detail?: string }>(
      `/api/background/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  },

  // Delete a FINISHED background job's entry (housekeeping). Running jobs are kept.
  deleteBackground(jobId: string) {
    return request<{ ok: boolean; deleted?: boolean }>(
      `/api/background/${encodeURIComponent(jobId)}`,
      { method: "DELETE" },
    );
  },

  // Delete all FINISHED background jobs (clears the stacked-up history).
  clearFinishedBackground() {
    return request<{ ok: boolean; cleared?: number }>("/api/background/clear", { method: "POST" });
  },

  subagents() {
    return request<{ subagents: Subagent[] }>("/api/subagents");
  },

  tools() {
    // `count` = wired (enabled) tools; `disabled` = the RAW tools.disabled denylist —
    // the base a row toggle edits, so stale names (no live tool) survive a save.
    return request<{ tools: ToolInfo[]; count: number; disabled: string[] }>("/api/tools");
  },

  runSubagent(body: {
    session_id: string;
    type: string;
    description: string;
    prompt: string;
  }) {
    return request<{ ok: boolean; session_id: string; output: string }>("/api/subagents/run", {
      method: "POST",
      body,
    });
  },

  runSubagentBatch(body: {
    session_id: string;
    tasks: Array<{
      type?: string;
      subagent_type?: string;
      description: string;
      prompt: string;
    }>;
  }) {
    return request<{ ok: boolean; session_id: string; output: string }>("/api/subagents/batch", {
      method: "POST",
      body,
    });
  },

  schedules() {
    return request<{ jobs: ScheduledJob[]; backend: string }>("/api/scheduler/jobs");
  },

  addSchedule(body: { prompt: string; schedule: string; job_id?: string; timezone?: string }) {
    return request<{ job: ScheduledJob }>("/api/scheduler/jobs", {
      method: "POST",
      body,
    });
  },

  // A PARTIAL update server-side (#3957): an omitted key keeps the job's current value,
  // so the console always sends `timezone` — `null` means "back to UTC", not "unchanged".
  updateSchedule(jobId: string, body: { prompt: string; schedule: string; timezone: string | null }) {
    return request<{ job: ScheduledJob }>(`/api/scheduler/jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    });
  },

  cancelSchedule(jobId: string) {
    return request<{ canceled: boolean }>(`/api/scheduler/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
    });
  },

  goals() {
    return request<{ goals: GoalState[]; enabled: boolean }>("/api/goals");
  },

  // One goal's full detail — the status dict + the durable plan artifact (`.plan.md`, the
  // agent's "orient" world-model it maintains via `update_goal_plan`, ADR 0079). `plan` is
  // "" when the goal hasn't recorded one. Powers the goal detail drawer.
  goalDetail(sessionId: string) {
    return request<{ enabled: boolean; goal: GoalState | null; plan: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}`,
    );
  },

  // Clear (stop) a goal. `closeTasks` also closes the goal's session-scoped task backlog
  // (ADR 0079) — the "Stop goal" action. Returns how many tasks were closed.
  clearGoal(sessionId: string, closeTasks = false) {
    const q = closeTasks ? "?close_tasks=true" : "";
    return request<{ cleared: boolean; tasks_closed?: number }>(
      `/api/goals/${encodeURIComponent(sessionId)}${q}`,
      { method: "DELETE" },
    );
  },

  // Goal lifecycle (ADR 0079) — re-arm: extend an active goal's iteration budget
  // (`add_iterations`), or reactivate a terminal one and kick a fresh drive turn (the backend
  // resets the loop + enqueues the turn). `resumed` is true when a terminal goal was
  // reactivated. A no-op (active goal, no added budget) comes back HTTP 400.
  rearmGoal(sessionId: string, body: { add_iterations?: number }) {
    return request<{ ok: boolean; message?: string; resumed?: boolean; kicked?: boolean; error?: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}/rearm`,
      { method: "POST", body },
    );
  },

  // Operator goal-set (ADR 0066) — the trusted operator channel. `/api` is operator-tier by
  // the ADR 0066 path ceiling, so this accepts ANY verifier type (unlike the plugin-only SDK
  // path). A rejected verifier / disabled goal mode comes back as HTTP 400 (request() throws,
  // so the caller's onError surfaces the reason); the happy path returns {ok:true, message}.
  // Optional completion-contract fields (ADR 0073) shape the drive-loop continuation
  // prompt each turn — the verifier still decides DONE. All optional and backward-compatible.
  setGoal(body: {
    session_id: string;
    condition: string;
    verifier: unknown;
    outcome?: string;
    constraints?: string[];
    boundaries?: string[];
    stop_when?: string;
    max_iterations?: number;
    // `false` = don't kick a headless drive turn; the caller drives the goal from a chat tab
    // instead (the console panel path). Omitted/true = the pre-tab behavior (auto-start).
    kick?: boolean;
  }) {
    return request<{ ok: boolean; message?: string; kicked?: boolean; error?: string }>("/api/goals", {
      method: "POST",
      body,
    });
  },

  // Detach-continue (ADR 0079): keep an ACTIVE goal driving in the background after the chat
  // tab that was streaming it is closed. 400 when the session has no active goal.
  resumeGoal(sessionId: string) {
    return request<{ ok: boolean; kicked?: boolean; error?: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}/resume`,
      { method: "POST" },
    );
  },

  // Watches (ADR 0067) — passive verifier-only objectives, many at once, keyed by id. The
  // panel invalidates this on the `watch.*` bus pushes (created/checked/met/expired/stalled)
  // instead of polling — same pattern as goals.
  watches() {
    return request<{ watches: WatchState[]; enabled: boolean }>("/api/watches");
  },

  // What a goal or watch can be checked WITH, from every source (ADR 0028/0067). The
  // creators build their verifier pickers from this instead of a hardcoded list — a UI-side
  // copy of a server registry drifts, and this one had already lost the whole `plugin` class.
  verifiers() {
    return request<VerifierCatalog>("/api/verifiers");
  },

  // Operator watch-create. This is the TRUSTED channel (ADR 0066 path ceiling), so unlike
  // the agent's plugin-only `create_watch` it accepts command/test/ci/data verifiers; a
  // rejected spec comes back 400 → `request` throws, which the caller surfaces as a toast.
  // `body` is the raw object — `request` serializes it (and sets the JSON content type).
  // Pre-stringifying here double-encodes it, which the mock server happily accepts and
  // FastAPI's `body: dict` rejects with a 422.
  createWatch(body: WatchCreateBody) {
    return request<{ ok: boolean; message?: string }>("/api/watches", {
      method: "POST",
      body,
    });
  },

  clearWatch(id: string) {
    return request<{ cleared: boolean }>(`/api/watches/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  // Workflows are an opt-in plugin (plugins/workflows) — it serves /api/plugins/workflows.
  workflows() {
    return request<{ workflows: WorkflowSummary[] }>("/api/plugins/workflows/list");
  },

  runWorkflow(name: string, inputs: Record<string, unknown>) {
    return request<WorkflowRunResult>(`/api/plugins/workflows/${encodeURIComponent(name)}/run`, {
      method: "POST",
      body: { inputs },
    });
  },

  // The Studio's run shape: validated up front (a bad request rejects here), then the
  // DAG executes detached — poll workflowRun(run_id) for the live per-step record.
  startWorkflow(name: string, inputs: Record<string, unknown>) {
    return request<{ started: boolean; run_id: string }>(
      `/api/plugins/workflows/${encodeURIComponent(name)}/start`,
      { method: "POST", body: { inputs } },
    );
  },

  // One run's full record — live polling target while it executes, history inspector after.
  workflowRun(runId: string) {
    return request<WorkflowRunRecord>(`/api/plugins/workflows/runs/${encodeURIComponent(runId)}`);
  },

  // Run history — summaries of every recorded run (any status), newest first.
  workflowRunHistory(limit = 50) {
    return request<{ runs: WorkflowRunSummary[] }>(`/api/plugins/workflows/runs/all?limit=${limit}`);
  },

  // The full recipe document — what the builder loads to EDIT (prompts, gates, output).
  workflowRecipe(name: string) {
    return request<{ recipe: WorkflowRecipe }>(
      `/api/plugins/workflows/${encodeURIComponent(name)}/recipe`,
    );
  },

  // Save's checks as data (never a 400) — the builder's live validation.
  validateWorkflow(recipe: Record<string, unknown>) {
    return request<{ errors: string[] }>("/api/plugins/workflows/validate", {
      method: "POST",
      body: recipe,
    });
  },

  // Resume a paused run detached (the Studio timeline's shape): prechecked up front,
  // then poll workflowRun(run_id) — the sync resumeWorkflow below returns the final
  // result directly and stays for the Pending Gates cards.
  resumeWorkflowBackground(
    runId: string,
    body: { action: "approve" | "edit" | "reject"; edits?: { prompt?: string } },
  ) {
    return request<{ resumed: boolean; run_id: string }>(
      `/api/plugins/workflows/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body: { ...body, background: true } },
    );
  },

  saveWorkflow(recipe: Record<string, unknown>) {
    return request<{ saved: boolean; name: string; path?: string }>("/api/plugins/workflows/save", {
      method: "POST",
      body: recipe,
    });
  },

  deleteWorkflow(name: string) {
    return request<{ deleted: boolean }>(`/api/plugins/workflows/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
  },

  // Paused workflow runs (F3) — runs parked at a `gate: human` step, awaiting operator
  // approval. The "Pending Gates" section polls this on mount + after each action.
  workflowRuns() {
    return request<{ runs: WorkflowPausedRun[] }>("/api/plugins/workflows/runs");
  },

  // Continue a paused run from its gated step: approve (original prompt), edit
  // (`edits.prompt` runs verbatim), or reject (step marked failed, DAG continues).
  // Resolves with the run's final output (or a paused envelope if a downstream gate hits).
  resumeWorkflow(
    runId: string,
    body: { action: "approve" | "edit" | "reject"; edits?: { prompt?: string } },
  ) {
    return request<WorkflowRunResult>(
      `/api/plugins/workflows/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body },
    );
  },

  // Tasks are agent-global (one persistent store) — no project scope. (Notes moved
  // to the first-party `notes` plugin, ADR 0034 S4 — it owns its own data route.)
  tasksStatus() {
    return request<{ initialized: boolean }>("/api/tasks/status");
  },

  initTasks() {
    return request<{ initialized: boolean; already_initialized?: boolean }>("/api/tasks/init", {
      method: "POST",
      body: {},
    });
  },

  tasks() {
    return request<{ issues: Task[] }>("/api/tasks/issues");
  },

  createTask(issue: {
    title: string;
    type?: string;
    priority?: number;
    description?: string;
    assignee?: string;
  }) {
    return request<{ issue: Task }>("/api/tasks/issues", {
      method: "POST",
      body: { ...issue },
    });
  },

  updateTask(
    issueId: string,
    update: {
      title?: string;
      description?: string;
      status?: string;
      priority?: number;
      type?: string;
      assignee?: string;
    },
  ) {
    return request<{ issue: Task }>(`/api/tasks/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      body: { ...update },
    });
  },

  closeTask(issueId: string, reason?: string) {
    return request<{ issue: Task }>(`/api/tasks/issues/${encodeURIComponent(issueId)}/close`, {
      method: "POST",
      body: { reason },
    });
  },

  deleteTask(issueId: string) {
    return request<{ deleted?: string; project_path?: string }>(
      `/api/tasks/issues/${encodeURIComponent(issueId)}`,
      { method: "DELETE" },
    );
  },
};
