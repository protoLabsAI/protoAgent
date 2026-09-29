"""Background maintenance loops and the curation passes they host.

Extracted from ``server/agent_init.py`` (#3807, epic #3804). These are the
long-lived ``asyncio`` tasks ``server/__init__``'s lifespan starts — checkpoint
prune (plus the telemetry / inbox / activity / A2A-task retention sweeps it
hosts), the out-of-band watch tick, the orphaned-A2A-task reaper, the RSS memory
guard, opt-in plugin auto-update, and the secrets-manager refresh — together with
the pieces only they drive: ``_retire_thread`` (harvest + delete, shared with the
delete-chat route), the persona-drift curation pass, and the untooled-action
persona audit (run at boot/reload by ``agent_init``).

Everything here reads the live ``runtime.state.STATE`` each pass, so a config
reload re-paces a loop without restarting it. ``server.agent_init`` (and
``server/__init__``) re-export these names so existing callers keep resolving.

**Patch here, not on ``agent_init``.** Collaborators these functions call by bare
name — ``_server_is_idle``, ``_run_soul_drift_pass``, ``_event_bus`` — resolve in
THIS module's globals, and the drift gate's ``_last_soul_drift_check`` lives only
here (deliberately not re-exported: a rebound float copy on ``agent_init`` would
silently decouple). The one exception is plugin auto-update's reload, which calls
``server.agent_init._apply_settings_changes`` through the module at call time — the
settings-apply path is defined in ``server.settings_apply`` (#3848) but its patch seam
stays agent_init's, and so are the patches that fake it.
"""

import asyncio
import logging
import os

from runtime.state import STATE
from server import _event_bus

log = logging.getLogger("protoagent.server")


async def _checkpoint_prune_loop() -> None:
    """Periodically trim the SQLite checkpoint DB (per-thread cap + age TTL).

    Reads the path + knobs from the live globals each pass so a config reload
    takes effect without restarting the loop. Failures are logged, never fatal.
    """
    import asyncio

    from graph.checkpoint_prune import find_aged_threads, prune_checkpoints

    await asyncio.sleep(60)  # let boot settle before the first sweep
    while True:
        cfg = STATE.graph_config
        path = STATE.checkpoint_path
        interval_h = getattr(cfg, "checkpoint_prune_interval_hours", 0) if cfg else 0
        if path and cfg and interval_h > 0:
            try:
                max_age = cfg.checkpoint_max_age_days * 86400 if cfg.checkpoint_max_age_days else None
                harvest = bool(
                    max_age
                    and cfg.checkpoint_harvest_enabled
                    and STATE.knowledge_store is not None
                    and STATE.checkpointer is not None
                )
                if harvest:
                    # Summarize each aged thread into knowledge, then drop it —
                    # past conversations stay searchable, raw checkpoints freed.
                    # Jittered gap between threads (#2946): back-to-back harvest model
                    # calls manufacture 429 bursts on a shared provider account (the
                    # standard fleet configuration) — and a failed harvest used to cost
                    # the conversation its knowledge forever.
                    import random as _random

                    for i, thread_id in enumerate(await asyncio.to_thread(find_aged_threads, path, max_age)):
                        if i:
                            await asyncio.sleep(_random.uniform(2.0, 5.0))
                        await _retire_thread(thread_id)
                # Per-thread cap on the survivors (SQL age-TTL is the fallback
                # delete path when harvesting is off).
                res = await asyncio.to_thread(
                    prune_checkpoints,
                    path,
                    keep_per_thread=cfg.checkpoint_keep_per_thread,
                    max_age_seconds=(None if harvest else max_age),
                    background_keep=cfg.checkpoint_background_keep,
                )
                if res["threads_deleted"] or res["checkpoints_deleted"]:
                    log.info(
                        "[checkpoint-prune] removed %d idle thread(s), %d old checkpoint(s)",
                        res["threads_deleted"],
                        res["checkpoints_deleted"],
                    )
                    # Reclaim freed space back to the OS (compact WAL + pages).
                    if getattr(cfg, "checkpoint_vacuum", True):
                        try:
                            from graph.checkpoint_prune import reclaim as _reclaim

                            vac = await asyncio.to_thread(_reclaim, path)
                            if vac["wal_truncated"] or vac["pages_reclaimed"]:
                                log.info(
                                    "[checkpoint-prune] reclaimed WAL=%d pages=%d",
                                    vac["wal_truncated"],
                                    vac["pages_reclaimed"],
                                )
                        except Exception:
                            log.exception("[checkpoint-prune] reclaim failed")
            except Exception:
                log.exception("[checkpoint-prune] sweep failed")
        # Telemetry retention guardrail (ADR 0006) — drop turns older than the
        # configured window so the per-turn store can't grow unbounded. 0 = keep all.
        keep_days = getattr(cfg, "telemetry_retention_days", 0) if cfg else 0
        if STATE.telemetry_store is not None and keep_days > 0:
            try:
                removed = await asyncio.to_thread(STATE.telemetry_store.prune, keep_days)
                if removed:
                    log.info("[telemetry-prune] removed %d turn(s) older than %dd", removed, keep_days)
            except Exception:
                log.exception("[telemetry-prune] sweep failed")
        # Inbox retention — delete delivered items older than the configured window
        # so the inbox DB can't grow unbounded. Pending (undelivered) items are never
        # pruned. 0 = keep all.
        inbox_keep = getattr(cfg, "inbox_retention_days", 0) if cfg else 0
        if STATE.inbox_store is not None and inbox_keep > 0:
            try:
                removed = await asyncio.to_thread(STATE.inbox_store.prune, inbox_keep)
                if removed:
                    log.info("[inbox-prune] removed %d delivered item(s) older than %dd", removed, inbox_keep)
            except Exception:
                log.exception("[inbox-prune] sweep failed")
        # Activity retention — delete feed entries older than the configured window
        # so the activity DB can't grow unbounded. 0 = keep all.
        activity_keep = getattr(cfg, "activity_retention_days", 0) if cfg else 0
        if STATE.activity_log is not None and activity_keep > 0:
            try:
                removed = await asyncio.to_thread(STATE.activity_log.prune, activity_keep)
                if removed:
                    log.info("[activity-prune] removed %d entry(ies) older than %dd", removed, activity_keep)
            except Exception:
                log.exception("[activity-prune] sweep failed")
        # A2A task TTL sweep (24h) — used to run only at boot, so an always-on
        # agent accumulated task rows forever between restarts. The store is
        # async (aiosqlite engine on this same loop), so it's awaited directly
        # rather than dispatched to a thread like the sync sqlite neighbors.
        if STATE.a2a_task_engine is not None:
            try:
                from a2a_impl.stores import sweep_expired_tasks

                swept = await sweep_expired_tasks(STATE.a2a_task_engine)
                if swept:
                    log.info("[a2a-task-prune] removed %d expired task record(s) (24h TTL)", swept)
                # Drop push-notification configs orphaned by the task sweep (ADR 0051).
                if STATE.a2a_push_engine is not None:
                    from a2a_impl.stores import sweep_orphaned_push_configs

                    orphaned = await sweep_orphaned_push_configs(STATE.a2a_task_engine, STATE.a2a_push_engine)
                    if orphaned:
                        log.info("[a2a-task-prune] removed %d orphaned push-config(s)", orphaned)
            except Exception:
                log.exception("[a2a-task-prune] sweep failed")
        # Persona drift curation (#1986) — a read-only diff of the live SOUL.md
        # against its earliest recorded baseline snapshot; publishes
        # persona.drift_detected when the deterministic drift score crosses the
        # threshold. Self-gated to its own soul.drift.interval_hours cadence and
        # best-effort, so it never blocks the prune sweep.
        try:
            await asyncio.to_thread(_maybe_run_soul_drift_pass, cfg)
        except Exception:
            log.exception("[soul-drift] pass failed")
        # Tick at the checkpoint interval if set, else hourly (so telemetry pruning
        # still runs when checkpoint pruning is off).
        await asyncio.sleep(max(1, interval_h or 1) * 3600)


async def _watch_loop() -> None:
    """Out-of-band cadence for watches (ADR 0067): periodically run each active watch's
    verifier — no agent turn — so a met watch reacts (run_in_session + hooks) without waiting
    for a session turn. Many watches tick together; verifier-only.

    The cadence is re-read from config on EVERY iteration, so editing ``watches.interval``
    in Settings retimes the loop on the next tick with no restart."""
    from graph.watches import DEFAULT_WATCH_INTERVAL_S, MIN_WATCH_INTERVAL_S

    await asyncio.sleep(15)  # let boot settle before the first tick
    while True:
        ctrl = STATE.watch_controller
        cfg = STATE.graph_config
        interval = getattr(cfg, "watch_interval", DEFAULT_WATCH_INTERVAL_S) if cfg else DEFAULT_WATCH_INTERVAL_S
        if ctrl is not None:
            try:
                n = await ctrl.tick_all()
                if n:
                    log.info("[watch] %d watch(es) reached a terminal state", n)
            except Exception:
                log.exception("[watch] tick failed")
        await asyncio.sleep(max(MIN_WATCH_INTERVAL_S, float(interval or DEFAULT_WATCH_INTERVAL_S)))


# A2A orphaned-WORKING-task reaper cadence (#3418). Deliberately far shorter than the
# checkpoint-prune hour, and finite (the console waits INDEFINITELY on a WORKING durable
# record): a producer that vanished without tripping boot reconciliation or the executor
# stall guard settles the spinner within minutes, not never.
A2A_REAPER_INTERVAL_S = 300.0


async def _a2a_reaper_loop() -> None:
    """Periodically fail orphaned WORKING A2A tasks (#3418).

    Boot reconciliation (``reconcile_interrupted_tasks``) covers a restart and the
    executor stall guard (``_stall_guarded``) covers an alive-but-silent stream; this
    loop covers the hole between them — a producer that vanished without tripping
    either, leaving a task stuck in ``TASK_STATE_WORKING`` and the console spinner
    permanent. The discriminator (orphan-at-birth vs. idle-productive) and the terminal
    transition live in ``reap_orphaned_working_tasks``; here we just tick on a fixed
    cadence and isolate failures so a reaper error can never harm chat service.

    Started unconditionally like ``_watch_loop`` / ``_memory_guard_loop`` and self-guards
    on ``STATE.a2a_task_engine`` (no-op until the durable stores exist, or when A2A is off).
    """
    from a2a_impl.stores import reap_orphaned_working_tasks, reap_thresholds_for

    await asyncio.sleep(45)  # let boot settle (and boot reconciliation run) before the first sweep
    while True:
        if STATE.a2a_task_engine is not None:
            try:
                # Read per sweep, not once: the stall timeout is live-reloadable, and a
                # value captured at boot would keep reaping on the old window after a
                # settings change. Passing the DERIVED pair rather than the raw timeout
                # keeps the ratio rule in one place (stores.reap_thresholds_for).
                stall = getattr(STATE.graph_config, "turn_stall_timeout_seconds", None)
                birth_grace_s, idle_after_s = reap_thresholds_for(stall)
                n = await reap_orphaned_working_tasks(
                    STATE.a2a_task_engine,
                    birth_grace_s=birth_grace_s,
                    idle_after_s=idle_after_s,
                )
                if n:
                    log.info("[a2a-reaper] failed %d orphaned WORKING task(s) (#3418)", n)
            except Exception:
                log.exception("[a2a-reaper] sweep failed")
        await asyncio.sleep(A2A_REAPER_INTERVAL_S)


MEMORY_GUARD_INTERVAL_S = 60.0


async def _memory_guard_loop() -> None:
    """Watch this process's own RSS against ``runtime.memory_ceiling_mb`` (#3365).

    Reads the ceiling from the live config every pass, like the other cadence loops,
    so changing it in Settings retimes the guard without a restart. Off (0) is the
    default and costs one attribute read a minute.

    Exiting on breach is opt-in (``runtime.memory_ceiling_exit``) — see
    ``infra/memory_guard.py`` for why that isn't the default.
    """
    from infra.memory_guard import EXIT_CODE, MemoryCeiling, read_rss_bytes

    await asyncio.sleep(30)  # let boot settle; a cold process is at its noisiest
    guard = MemoryCeiling(0)
    while True:
        cfg = STATE.graph_config
        # Build a candidate from the live config and compare its NORMALIZED fields.
        # Never coerce the raw YAML values here: `int()` raises on float("inf") and
        # `bool("false")` is True — for a knob that ends the process, parsing has to
        # happen in exactly one place, and that place is MemoryCeiling.
        candidate = MemoryCeiling(
            getattr(cfg, "memory_ceiling_mb", 0) if cfg else 0,
            exit_on_breach=getattr(cfg, "memory_ceiling_exit", False) if cfg else False,
        )
        if (candidate.ceiling_bytes, candidate.exit_on_breach) != (guard.ceiling_bytes, guard.exit_on_breach):
            guard = candidate  # config changed → adopt it, and the breach latch resets with it
        if guard.enabled:
            try:
                # Off the event loop: the macOS reader shells out to `ps`, and this
                # loop shares a thread with every request the server is serving.
                action, message = guard.evaluate(await asyncio.to_thread(read_rss_bytes))
                if action == "warn":
                    log.warning("%s", message)
                elif action == "exit":
                    log.error("%s", message)
                    # os._exit, not sys.exit: this runs on the event loop, where a
                    # SystemExit would be swallowed as a task exception and the
                    # process would sail past its own ceiling.
                    os._exit(EXIT_CODE)
            except Exception:  # noqa: BLE001 — the guard must never take down the loop it guards
                log.exception("[memory] guard check failed")
        await asyncio.sleep(MEMORY_GUARD_INTERVAL_S)


# Consecutive failed harvests per thread (#2946) — in-process only: a restart resets
# the count, which just grants a fresh retry budget. Entries clear on success or delete.
_HARVEST_FAILURES: dict[str, int] = {}
_HARVEST_FAILURE_CAP = 3


async def _retire_thread(thread_id: str, *, harvest: bool | None = None, cascade: bool = True) -> str | None:
    """Harvest a thread to the knowledge base (best-effort) then delete its
    checkpoints. Shared by the prune sweep and explicit tab deletion. Returns
    the harvested knowledge chunk id, if any.

    ``harvest`` — ``None`` defers to ``checkpoint_harvest_enabled`` (the TTL
    sweep's config-driven default); an explicit bool overrides it (the
    delete-chat dialog's opt-in checkbox: an unchecked box must not harvest
    just because the sweep is configured to, and a checked box is an explicit
    operator request).

    ``cascade`` — when True (the default), also deletes any
    ``:goal-iter-N`` sub-threads so goal-mode iteration checkpoints are not
    orphaned."""
    import asyncio

    from graph.checkpoint_prune import delete_thread

    chunk_id = None
    do_harvest = getattr(STATE.graph_config, "checkpoint_harvest_enabled", False) if harvest is None else harvest
    if STATE.graph_config is not None and do_harvest:
        from graph.conversation_harvest import harvest_thread

        try:
            chunk_id = await harvest_thread(
                thread_id,
                checkpointer=STATE.checkpointer,
                knowledge_store=STATE.knowledge_store,
                config=STATE.graph_config,
                raise_on_error=True,
            )
        except Exception:
            # A FAILED harvest must not silently cost the conversation its knowledge
            # (#2946): the old flow swallowed the error and deleted the thread anyway —
            # a transient 429 at retire time (the shared-account burst case) permanently
            # skipped capture. Sweep-path failures are retryable: keep the thread for
            # the next sweep, up to a cap so a permanently-broken harvest can't pin
            # checkpoints forever. An EXPLICIT delete (the dialog's checkbox) still
            # deletes — the operator asked for deletion — but says so loudly.
            if harvest is None:
                n = _HARVEST_FAILURES.get(thread_id, 0) + 1
                if n < _HARVEST_FAILURE_CAP:
                    _HARVEST_FAILURES[thread_id] = n
                    log.warning(
                        "[retire] harvest failed for %s (attempt %d/%d) — keeping the thread "
                        "for the next sweep",
                        thread_id,
                        n,
                        _HARVEST_FAILURE_CAP,
                    )
                    return None
                _HARVEST_FAILURES.pop(thread_id, None)
                log.error(
                    "[retire] harvest failed %d times for %s — deleting anyway; its knowledge "
                    "was NOT captured",
                    n,
                    thread_id,
                )
            else:
                log.warning(
                    "[retire] harvest failed for %s — explicit delete proceeds; its knowledge "
                    "was NOT captured",
                    thread_id,
                )
        else:
            _HARVEST_FAILURES.pop(thread_id, None)
    if STATE.checkpoint_path:
        await asyncio.to_thread(delete_thread, STATE.checkpoint_path, thread_id, cascade=cascade)
    elif STATE.checkpointer is not None and hasattr(STATE.checkpointer, "delete_thread"):
        try:
            STATE.checkpointer.delete_thread(thread_id)
        except Exception:
            log.exception("[retire] in-memory delete_thread failed for %s", thread_id)
    # The trajectory outlives checkpoint PRUNING but not thread RETIREMENT
    # (ADR 0102 D3) — same lifetime as the thread's own existence.
    try:
        from observability.trajectory import trajectory_log

        trajectory_log.retire(thread_id)
    except Exception:  # noqa: BLE001 — retirement cleanup is best-effort
        log.debug("[retire] trajectory retire failed for %s", thread_id, exc_info=True)
    return chunk_id


# ── Persona drift curation (#1986) ───────────────────────────────────────────
# A read-only curation pass (lineage of the dream/distill maintenance passes):
# periodically diff the live SOUL.md against its earliest recorded soul-history
# snapshot and, when the deterministic drift score crosses ``soul.drift.threshold``,
# publish ``persona.drift_detected`` on the event bus. It never rewrites the
# persona — recovery already exists via the restore endpoint (ADR 0081); this only
# surfaces the signal. Hosted by the checkpoint-prune loop below (no new startup
# wiring), but gated to its own ``soul.drift.interval_hours`` cadence so a fast
# prune interval doesn't over-run it.
_last_soul_drift_check: float = 0.0


def _judge_soul_drift(cfg, report: dict) -> dict | None:
    """Run the opt-in semantic tier for a report that already crossed the threshold.

    Returns the judge's ``{drift_score, identity_preserved, doctrine_leak, rationale}``,
    or ``None`` when the tier is off, the baseline can't be re-read, or the judge gave no
    usable verdict. Best-effort throughout: the deterministic report stands on its own and
    a judge failure must not cost us the signal we already have."""
    if not getattr(cfg, "soul_drift_judge_enabled", False):
        return None
    try:
        from graph.config_io import read_soul, read_soul_version
        from graph.soul_judge import judge_soul_drift

        baseline = read_soul_version(report.get("baseline_id") or "")
        current = read_soul()
        if not baseline or not current:
            return None
        verdict = judge_soul_drift(baseline, current, model=(getattr(cfg, "soul_drift_judge_model", "") or None))
    except Exception:  # noqa: BLE001 — never break the pass over the optional tier
        log.exception("[soul-drift] semantic tier failed")
        return None
    if verdict:
        log.info(
            "[soul-drift] semantic verdict: identity_preserved=%s doctrine_leak=%s score=%.3f — %s",
            verdict["identity_preserved"],
            verdict["doctrine_leak"],
            verdict["drift_score"],
            verdict["rationale"],
        )
    return verdict


def _run_soul_drift_pass(cfg) -> dict | None:
    """Run one persona-drift curation pass NOW (ignores the interval gate).

    Returns the drift report when a comparison ran (whether or not it crossed the
    threshold), else ``None`` (feature off, or nothing to compare against yet).
    Publishes ``persona.drift_detected`` — carrying the score, the individual
    signals, and a human-readable rationale — only when the score is at or above
    ``soul.drift.threshold``. Best-effort: detection/publish failures are logged,
    never raised into the hosting loop."""
    if cfg is None or not getattr(cfg, "soul_drift_enabled", False):
        return None
    try:
        from graph.config_io import detect_soul_drift

        report = detect_soul_drift()
    except Exception:
        log.exception("[soul-drift] detection failed")
        return None
    if report is None:
        return None  # no baseline snapshot / no live persona — nothing to compare

    threshold = float(getattr(cfg, "soul_drift_threshold", 1.0) or 0.0)
    if report["score"] >= threshold:
        # Semantic tier (#2272) — only past the deterministic threshold: the cheap signal
        # decides WHETHER to look, the judge decides WHAT KIND. Retention can't separate
        # "the persona was rewritten" from "operating instructions accreted into it" —
        # both read as a low ratio — so `doctrine_leak` needs a semantic judgement.
        semantic = _judge_soul_drift(cfg, report)
        if semantic:
            report["semantic"] = semantic
        try:
            _event_bus.publish(
                "persona.drift_detected",
                {
                    "score": report["score"],
                    "threshold": threshold,
                    "baseline_id": report.get("baseline_id"),
                    "baseline_saved_at": report.get("baseline_saved_at"),
                    "signals": {
                        "retention": report["retention"],
                        "size_delta": report["size_delta"],
                        "baseline_size": report["baseline_size"],
                        "current_size": report["current_size"],
                        "sections_added": report["sections_added"],
                        "sections_dropped": report["sections_dropped"],
                    },
                    "rationale": report["rationale"],
                    # Absent when the tier is off OR the judge gave no usable verdict —
                    # a distinct state from "clean", so a consumer can't read silence as
                    # a semantic all-clear it never received.
                    **({"semantic": semantic} if semantic else {}),
                },
            )
            log.info(
                "[soul-drift] persona.drift_detected score=%.3f (threshold %.3f) — %s",
                report["score"],
                threshold,
                report["rationale"],
            )
        except Exception:
            log.exception("[soul-drift] event publish failed")
    return report


def _maybe_run_soul_drift_pass(cfg) -> dict | None:
    """Gate :func:`_run_soul_drift_pass` to its own ``soul.drift.interval_hours``
    cadence, then run it. Called every prune tick; a no-op until an interval has
    elapsed since the last run (tracked on a monotonic module global so a config
    reload re-paces without restarting the loop). ``interval_hours`` ``0`` disables."""
    global _last_soul_drift_check
    if cfg is None or not getattr(cfg, "soul_drift_enabled", False):
        return None
    interval_h = getattr(cfg, "soul_drift_interval_hours", 0) or 0
    if interval_h <= 0:
        return None
    import time

    now = time.monotonic()
    if _last_soul_drift_check and (now - _last_soul_drift_check) < interval_h * 3600:
        return None
    _last_soul_drift_check = now
    return _run_soul_drift_pass(cfg)


def _audit_persona_tools(graph, *, trigger: str) -> None:
    """Untooled-action audit (#2276) — warn when the live persona commits to actions no
    bound tool backs, because the model fills an untooled instruction with narration and
    reports it done (no error is ever raised; the breakage is invisible in-band).

    Runs at the two moments the persona/tool pairing changes — boot and reload — over the
    graph's stamped ``bound_tools``. Warn-only by design: one log line per finding plus a
    single ``persona.untooled_action_detected`` bus event carrying them all (sibling of
    ``persona.drift_detected``). Never raises — an audit must not cost a boot or reload —
    and never blocks the persona from loading: wanting a tool before configuring it is a
    legitimate state, so detection stays passive until a guarded tier is a real ask."""
    if graph is None:  # setup pending — no tools bound, nothing to diff against
        return
    try:
        from graph.config_io import read_soul, soul_revision
        from graph.soul_audit import audit_untooled_actions

        soul = read_soul()
        names = [getattr(t, "name", str(t)) for t in getattr(graph, "bound_tools", None) or ()]
        if not soul or not names:
            return
        findings = audit_untooled_actions(soul, names)
        if not findings:
            return
        for f in findings:
            log.warning(
                '[soul-audit] persona commits to an action no bound tool backs — %s %r ("%s"). '
                "The model will narrate this as done rather than fail; register/enable the tool "
                "or edit SOUL.md.",
                f["kind"],
                f["action"],
                f["evidence"],
            )
        _event_bus.publish(
            "persona.untooled_action_detected",
            {
                "trigger": trigger,
                "soul_revision": soul_revision(),
                "count": len(findings),
                "findings": findings,
            },
        )
    except Exception:
        log.exception("[soul-audit] untooled-action audit failed")


# ── Opt-in plugin auto-update (#1720) ────────────────────────────────────────
# Only plugins the operator lists in ``plugins.update_policy`` are ever touched;
# a pinned-to-SHA plugin is never auto-updated. ``when: idle`` defers a plugin's
# update while a chat turn is (or was just) in flight — the reload rebuilds
# tools/routers, safe between turns but disruptive during one.
_AUTOUPDATE_IDLE_QUIET_S = 300.0  # "idle" = no chat turn started in this window


def _server_is_idle() -> bool:
    """True when no chat turn is in flight AND none finished within the quiet window
    (the ``when: idle`` gate). Reads the beacon ``server.chat`` maintains around
    every turn; if that import fails we conservatively report NOT idle so we never
    reload mid-turn."""
    try:
        from server.chat import active_turns, seconds_since_last_turn

        return active_turns() == 0 and seconds_since_last_turn() >= _AUTOUPDATE_IDLE_QUIET_S
    except Exception:
        return False


async def _autoupdate_one_plugin(plugin_id: str, entry: dict, status: dict, cfg, enabled: list[str]) -> None:
    """Pull + hot-reload one plugin, mirroring the console Update route
    (``operator_api/plugin_routes.py``): resolve the target ref (a release-tag pin
    moves to the newest tag; a branch pulls its head), re-install with ``force``,
    then — if the plugin is enabled — purge its modules and reload through the
    enable path so the fresh code mounts. Emits ``plugin.updated`` on the bus. Reads
    the allowlist / disabled set off the SAME ``cfg`` the sweep was handed."""
    import asyncio

    from graph.plugins import installer
    from graph.plugins.loader import purge_plugin_modules

    source_url = entry.get("source_url", "")
    if not source_url:
        return

    ref = entry.get("requested_ref", "") or None
    if ref and installer.is_release_tag(ref):
        # A release-tag pin is immutable — the update target is the newest semver
        # tag (the check's latest_ref), not the recorded one.
        ref = status.get("latest_ref") or ref

    allow = list(getattr(cfg, "plugins_sources_allow", []) or []) or None
    try:
        summary = await asyncio.to_thread(installer.install, source_url, ref, force=True, by="autoupdate", allow=allow)
    except installer.InstallError as exc:
        log.warning("[plugin-autoupdate] install failed for %s: %s", plugin_id, exc)
        return
    except Exception:
        log.exception("[plugin-autoupdate] install crashed for %s", plugin_id)
        return

    reloaded = False
    if plugin_id in enabled:
        # Force a genuinely fresh import of the just-pulled code, then reload
        # through the enable route's path (router re-mount, tools/MCP rebuild, #822).
        # Guard the whole block: the code + lock are already updated on disk, so a
        # reload crash must not abort the rest of the sweep (a bare raise here would
        # unwind past the per-plugin loop) — the new code mounts on the next
        # boot/enable regardless.
        try:
            purge_plugin_modules(plugin_id)
            disabled = list(getattr(cfg, "plugins_disabled", []) or [])
            # Through the module, not a from-import: the settings-apply path (and
            # the tests that fake it) live on agent_init, which imports THIS module
            # at load — a top-level import back would be a cycle.
            from server import agent_init

            ok, messages = agent_init._apply_settings_changes(
                config={"plugins": {"enabled": list(enabled), "disabled": disabled}},
            )
            reloaded = bool(ok)
            if not ok:
                log.error("[plugin-autoupdate] reload failed for %s: %s", plugin_id, "; ".join(messages))
        except Exception:
            log.exception(
                "[plugin-autoupdate] reload crashed for %s (update is on disk; next boot/enable mounts it)",
                plugin_id,
            )

    try:
        from server import _event_bus

        _event_bus.publish(
            "plugin.updated",
            {
                "id": plugin_id,
                "version": summary.get("version"),
                "resolved_sha": summary.get("resolved_sha"),
                "reloaded": reloaded,
                "by": "autoupdate",
            },
        )
    except Exception:
        log.exception("[plugin-autoupdate] event publish failed for %s", plugin_id)

    log.info(
        "[plugin-autoupdate] updated %s → %s (reloaded=%s)",
        plugin_id,
        (summary.get("resolved_sha") or "")[:8],
        reloaded,
    )


async def _plugin_autoupdate_sweep(cfg, policy: dict) -> int:
    """One pass over the update policy. For each opted-in, non-pinned, behind plugin
    at a safe moment, pull + reload it. Returns the number of plugins updated. Each
    plugin is guarded independently — one failure never blocks the rest."""
    import asyncio

    from graph.plugins import installer

    installed = {e.get("id"): e for e in installer.list_installed()}
    enabled = list(getattr(cfg, "plugins_enabled", []) or [])
    updated = 0
    for plugin_id, raw in policy.items():
        pol = raw if isinstance(raw, dict) else {}
        # ``track`` opts the plugin in (the ref itself comes from the lock); an
        # empty/missing track means the entry is present but not armed.
        if not str(pol.get("track") or "").strip():
            continue
        entry = installed.get(plugin_id)
        if entry is None:
            log.info("[plugin-autoupdate] %s in policy but not installed — skipping", plugin_id)
            continue
        if entry.get("superseded"):
            # Moved into core: the bundled copy runs and updates with protoAgent, and the
            # installed copy is ignored — pulling it would only trip the built-in guard,
            # which used to log an install failure on every sweep. The source is redacted
            # like every other place it's surfaced: an install URL can carry a token.
            from graph.plugins.manifest import display_source

            log.info(
                "[plugin-autoupdate] %s ships with protoAgent now (bundled v%s supersedes %s) — skipping; "
                "uninstall the ignored copy and drop it from plugins.update_policy",
                plugin_id,
                entry.get("bundled_version"),
                display_source(entry.get("source_url")),
            )
            continue
        when = str(pol.get("when") or "idle").strip().lower()
        if when != "always" and not _server_is_idle():
            log.info("[plugin-autoupdate] %s deferred — server busy (when=%s)", plugin_id, when)
            continue
        try:
            status = await asyncio.to_thread(installer.check_plugin_update, entry)
        except Exception:
            log.exception("[plugin-autoupdate] update check failed for %s", plugin_id)
            continue
        if status.get("error"):
            log.info("[plugin-autoupdate] %s check error: %s", plugin_id, status["error"])
            continue
        if status.get("pinned") or not status.get("behind"):
            continue
        await _autoupdate_one_plugin(plugin_id, entry, status, cfg, enabled)
        updated += 1
    return updated


async def _plugin_autoupdate_loop() -> None:
    """Periodically pull opt-in plugins that declare an update policy (#1720).

    Reads ``plugins.update_policy`` + ``plugins.autoupdate_interval_hours`` from the
    live config each pass, so a config reload takes effect without restarting the
    loop. Interval ``0`` or an empty policy map = idle (the default — nothing is
    touched). Failures are logged, never fatal."""
    import asyncio

    await asyncio.sleep(120)  # let boot settle past the first turn before sweeping
    while True:
        cfg = STATE.graph_config
        interval_h = getattr(cfg, "plugins_autoupdate_interval_hours", 0) if cfg else 0
        policy = getattr(cfg, "plugins_update_policy", {}) if cfg else {}
        if cfg and interval_h > 0 and policy:
            try:
                n = await _plugin_autoupdate_sweep(cfg, policy)
                if n:
                    log.info("[plugin-autoupdate] sweep updated %d plugin(s)", n)
            except Exception:
                log.exception("[plugin-autoupdate] sweep failed")
        # Re-read the interval each pass so a config change re-paces the loop; a
        # disabled loop still wakes hourly to notice a re-enable.
        await asyncio.sleep((interval_h if interval_h > 0 else 1) * 3600)


async def _secrets_refresh_loop() -> None:
    """Re-pull env vars from the external secrets manager on its interval (ADR 0080
    D5) so rotation lands without a restart. Reads the live config each pass — a
    reload re-paces or en/disables the loop without restarting it; while disabled it
    idles on 5-minute checks. ``force=True`` bypasses the hydrator's TTL gate (this
    loop IS the TTL). ``required: true`` is a boot gate only — a refresh failure
    here warns and keeps the last-applied values."""
    import asyncio

    def _live() -> tuple[bool, int]:
        cfg = STATE.graph_config
        if cfg is None:
            return False, 0
        return (
            bool(getattr(cfg, "secrets_manager_enabled", False)),
            int(getattr(cfg, "secrets_manager_refresh_seconds", 0) or 0),
        )

    while True:
        enabled, refresh = _live()
        await asyncio.sleep(float(max(30, refresh)) if (enabled and refresh > 0) else 300.0)
        enabled, refresh = _live()  # re-read after the sleep — config may have changed
        if not (enabled and refresh > 0):
            continue
        from graph.config import load_config_docs
        from graph.config_io import config_yaml_path
        from infra.secrets import SecretsRequiredError, hydrate_from_docs

        try:
            merged, secrets_doc = load_config_docs(config_yaml_path())
            await asyncio.to_thread(hydrate_from_docs, merged, secrets_doc, force=True)
        except SecretsRequiredError as e:
            log.warning("[secrets] refresh failed (required source): %s", e)
        except Exception:
            log.exception("[secrets] refresh failed")
