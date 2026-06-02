"""Eval runner — executes ``tasks.json``, prints a pass/fail board,
writes a JSON report to ``evals/results/run-<ts>.json``.

Usage:

.. code:: bash

    # agent must be running at $EVAL_BASE_URL (default http://localhost:7870)
    # auth: $A2A_AUTH_TOKEN and/or $<AGENT_NAME>_API_KEY (or $EVAL_API_KEY)

    python -m evals.runner                                # all cases
    python -m evals.runner --category tool                # one category
    python -m evals.runner --tasks current_time,daily_log
    python -m evals.runner --base-url http://host:7870

Cases are described in ``tasks.json``. Each case picks one of three
``kind`` runners:

- ``agent_card`` — fetch ``/.well-known/agent-card.json`` and assert
  on the returned card shape.
- ``auth_check`` — send a request with a known-bad bearer token and
  assert the expected HTTP status.
- ``ask`` — send a prompt over A2A, optionally pre-seed the KB, then
  assert against three independent channels: audit-log tool firing,
  reply-text patterns, and KB side effects.

A case passes only when all assertions hold. The ``detail`` column in
the pass/fail board names the missing assertion when one fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Allow ``python -m evals.runner`` and ``python evals/runner.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.client import AgentClient, TaskResult
from evals import verify


@dataclass
class CaseResult:
    id: str
    category: str
    name: str
    passed: bool
    detail: str
    duration_ms: int = 0
    tokens: int = 0
    raw: dict = field(default_factory=dict)


# ── case runners ────────────────────────────────────────────────────────────


async def _run_agent_card(client: AgentClient, case: dict) -> CaseResult:
    expect = case.get("expect", {})
    try:
        card = await client.agent_card()
    except Exception as e:
        return CaseResult(case["id"], case["category"], case["name"], False, f"fetch failed: {e}")

    problems: list[str] = []
    if "name" in expect and card.get("name") != expect["name"]:
        problems.append(f"name={card.get('name')!r} expected {expect['name']!r}")
    if "skills_min" in expect:
        skills = card.get("skills") or []
        if len(skills) < expect["skills_min"]:
            problems.append(f"only {len(skills)} skills, expected >= {expect['skills_min']}")
    if "extensions_contain" in expect:
        ext_uris = [
            e.get("uri", "")
            for e in (card.get("capabilities") or {}).get("extensions") or []
        ]
        for needle in expect["extensions_contain"]:
            if not any(needle in u for u in ext_uris):
                problems.append(f"missing extension matching {needle!r}; saw {ext_uris}")
    if problems:
        return CaseResult(case["id"], case["category"], case["name"], False, "; ".join(problems))
    return CaseResult(case["id"], case["category"], case["name"], True, "card OK")


async def _run_auth_check(client: AgentClient, case: dict) -> CaseResult:
    """Verify the A2A endpoint rejects a request with the expected status.

    Default behaviour exercises bearer auth alone using ``case["bad_token"]``.
    Cases can override headers via ``case["headers"]`` to test other
    auth surfaces — e.g. ``{"X-API-Key": "wrong"}`` for the legacy
    X-API-Key path. ``Content-Type: application/json`` is always set
    for the eval client; case headers override anything else.
    """
    import httpx

    expected_status = case.get("expect", {}).get("status", 401)
    bad = case.get("bad_token", "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bad}",
    }
    headers.update(case.get("headers") or {})
    payload = {
        "jsonrpc": "2.0",
        "id": "auth-check",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "ping"}],
                "messageId": "auth-check",
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{client.base_url}/a2a", headers=headers, json=payload)
    except Exception as e:
        return CaseResult(case["id"], case["category"], case["name"], False, f"request failed: {e}")
    if r.status_code != expected_status:
        return CaseResult(
            case["id"], case["category"], case["name"], False,
            f"got {r.status_code}, expected {expected_status}",
        )
    return CaseResult(
        case["id"], case["category"], case["name"], True, f"status={r.status_code}",
    )


async def _run_ask(client: AgentClient, case: dict) -> CaseResult:
    """Send via ``message/send`` + poll. Teardown always runs."""
    return await _run_prompt_case(client, case, streaming=False)


async def _run_stream(client: AgentClient, case: dict) -> CaseResult:
    """Send via ``message/stream`` + SSE. Same assertion shape as ``ask``,
    plus an optional ``expected_event_kinds`` list that asserts the SSE
    stream surfaced the named event kinds (``status-update``, ``task``,
    etc.) at least once."""
    return await _run_prompt_case(client, case, streaming=True)


_AUDIT_POLL_DEADLINE_S = 2.0
_AUDIT_POLL_INTERVAL_S = 0.05


async def _await_audit_assertion(
    since: str,
    expected_tools: list[str],
    *,
    require_success: bool,
) -> tuple[list[dict], bool, str]:
    """Poll the audit log until ``expected_tools`` have all fired (or the
    deadline is hit). Returns ``(entries, passed, detail)``.

    Replaces a fixed ``asyncio.sleep`` — under audit-log contention the
    fixed wait was sometimes shorter than the flush, causing flaky
    tool-firing assertions. Polling exits as soon as the assertion
    passes; the deadline only kicks in when the tool genuinely never
    fired.
    """
    deadline = asyncio.get_running_loop().time() + _AUDIT_POLL_DEADLINE_S
    entries: list[dict] = []
    passed = False
    detail = ""
    while True:
        entries = verify.audit_entries_since(since)
        passed, detail = verify.assert_tools_fired(
            entries, expected_tools, require_success=require_success,
        )
        if passed or asyncio.get_running_loop().time() >= deadline:
            return entries, passed, detail
        await asyncio.sleep(_AUDIT_POLL_INTERVAL_S)


async def _run_prompt_case(
    client: AgentClient,
    case: dict,
    *,
    streaming: bool,
) -> CaseResult:
    events: list[dict] = []
    result: TaskResult | None = None

    try:
        # Pre-seed state via direct DB writes (model never sees this).
        # Inside the ``try`` so a partial setup failure still triggers
        # the ``finally`` teardown — otherwise rows from the steps that
        # *did* succeed would leak into the next case.
        if "setup" in case:
            err = verify.apply_setup(case["setup"])
            if err:
                return CaseResult(
                    case["id"], case["category"], case["name"], False,
                    f"setup failed: {err}",
                )

        since = verify.audit_now()

        if streaming:
            events, result = await client.stream(
                case["prompt"], timeout_s=case.get("timeout_s", 90),
            )
        else:
            result = await client.ask(
                case["prompt"], timeout_s=case.get("timeout_s", 90),
            )

        if result is None or result.state != "completed":
            state = result.state if result else "no-final-event"
            error = (result.error if result else None) or "(none)"
            duration = result.duration_ms if result else 0
            text_preview = (result.text if result else "")[:200]
            return CaseResult(
                case["id"], case["category"], case["name"], False,
                f"task state={state}; error={error}",
                duration_ms=duration,
                raw={"text": text_preview},
            )

        problems: list[str] = []

        # Tool firing assertions. ``expected_tools is not None`` so an
        # explicit empty list asserts that *no* tools fired (abstention
        # cases). Missing key skips the audit check entirely.
        expected_tools = case.get("expected_tools")
        if expected_tools is not None:
            require_success = case.get("tool_outcome", "success") == "success"
            _entries, passed, detail = await _await_audit_assertion(
                since, expected_tools, require_success=require_success,
            )
            if not passed:
                problems.append(detail)

        # "Any of these tools fired" — for intent met equally by several tools
        # (e.g. delegated research via `task` OR `run_workflow`).
        any_tools = case.get("expected_any_tools")
        if any_tools:
            require_success = case.get("tool_outcome", "success") == "success"
            deadline = asyncio.get_running_loop().time() + _AUDIT_POLL_DEADLINE_S
            while True:
                entries = verify.audit_entries_since(since)
                passed, detail = verify.assert_any_tool_fired(
                    entries, any_tools, require_success=require_success,
                )
                if passed or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(_AUDIT_POLL_INTERVAL_S)
            if not passed:
                problems.append(detail)

        # Text pattern assertions (case-insensitive substrings).
        text_lower = result.text.lower()
        for pattern in case.get("expected_patterns") or []:
            if pattern.lower() not in text_lower:
                problems.append(f"missing pattern {pattern!r}")

        # KB side-effect assertions.
        vk = case.get("verify_kb") or {}
        if "find_chunk_containing" in vk:
            chunk = verify.find_chunk_containing(
                vk["find_chunk_containing"], domain=vk.get("domain"),
            )
            if not chunk:
                problems.append(f"no chunk containing {vk['find_chunk_containing']!r}")

        # LLM-judge rubric (for quality substring/audit can't judge).
        problems += _check_rubric(case, result.text)

        # Streaming-only: assert the SSE event sequence surfaced the
        # expected kinds at least once.
        if streaming:
            seen_kinds = {e.get("kind") for e in events}
            for kind in case.get("expected_event_kinds") or []:
                if kind not in seen_kinds:
                    problems.append(f"missing SSE event kind {kind!r}; saw {sorted(seen_kinds)}")

        detail = (
            "; ".join(problems) if problems
            else f"OK ({result.duration_ms}ms, {result.usage.get('total_tokens', '?')}t)"
        )
        return CaseResult(
            case["id"], case["category"], case["name"],
            passed=not problems,
            detail=detail,
            duration_ms=result.duration_ms,
            tokens=result.usage.get("total_tokens", 0) or 0,
            raw={"reply": result.text[:300]},
        )
    finally:
        # Teardown unconditionally — even when the task crashed or
        # an assertion raised — so seeded KB rows never leak into the
        # next case.
        if "teardown" in case:
            verify.apply_teardown(case["teardown"])


async def _run_goal_case(client: AgentClient, case: dict) -> CaseResult:
    """Goal-mode case: set a goal in a pinned session, send a trigger turn,
    then assert on the resulting goal state and the reply footer.

    Case schema (in addition to id/category/name):
      - ``set_goal``: the goal spec dict sent as ``/goal {json}`` (condition +
        verifier + optional max_iterations). Use deterministic ``command``
        verifiers (``"true"``/``"false"``) so the outcome is independent of
        model competence.
      - ``prompt``: the trigger message that runs the goal loop.
      - ``expected_goal_status``: ``achieved`` / ``exhausted`` / ``unachievable``
        — checked against ``GET /api/goal/{session}``.
      - ``expected_patterns``: substrings that must appear in the reply (the
        goal footer, e.g. ``goal achieved``).

    The goal is cleared before (clean slate) and after (teardown) the case.
    """
    cid = case.get("id", "goal")
    ctx = case.get("context_id") or f"eval-goal-{cid}"
    cat, name = case.get("category", "goal"), case.get("name", cid)
    try:
        await client.clear_goal(ctx)

        spec = case.get("set_goal")
        if not isinstance(spec, dict):
            return CaseResult(cid, cat, name, False, "case missing 'set_goal' spec")
        set_reply = await client.ask("/goal " + json.dumps(spec), timeout_s=30, context_id=ctx)
        if set_reply.state != "completed" or "goal set" not in set_reply.text.lower():
            return CaseResult(cid, cat, name, False, f"goal not set (state={set_reply.state}): {set_reply.text[:120]!r}")

        result = await client.ask(case["prompt"], timeout_s=case.get("timeout_s", 120), context_id=ctx)
        if result is None or result.state != "completed":
            state = result.state if result else "no-final-event"
            return CaseResult(cid, cat, name, False, f"trigger state={state}; error={(result.error if result else None) or '(none)'}")

        problems: list[str] = []

        expected_status = case.get("expected_goal_status")
        if expected_status:
            goal_resp = await client.get_goal(ctx)
            gstate = goal_resp.get("goal")
            if not isinstance(gstate, dict):
                # Fail loudly on a missing/empty goal record instead of letting
                # a None status quietly mismatch — surfaces a backend/shape
                # divergence rather than masking it.
                problems.append(f"expected goal status {expected_status!r} but no goal state returned (resp={goal_resp})")
            elif gstate.get("status") != expected_status:
                problems.append(
                    f"goal status={gstate.get('status')!r} expected {expected_status!r} "
                    f"(iter={gstate.get('iteration')}, reason={str(gstate.get('last_reason',''))[:80]!r})"
                )

        text_lower = result.text.lower()
        for pattern in case.get("expected_patterns") or []:
            if pattern.lower() not in text_lower:
                problems.append(f"missing pattern {pattern!r}")

        detail = "; ".join(problems) if problems else f"OK ({result.duration_ms}ms)"
        return CaseResult(
            cid, cat, name, passed=not problems, detail=detail,
            duration_ms=result.duration_ms,
            tokens=result.usage.get("total_tokens", 0) or 0,
            raw={"reply": result.text[:300]},
        )
    finally:
        try:
            await client.clear_goal(ctx)
        except Exception:
            pass


def _check_rubric(case: dict, text: str) -> list[str]:
    """Run a case's ``verify_rubric`` (if any) through the LLM judge; return a
    list of problems (empty when it passes or no rubric is configured)."""
    rubric = case.get("verify_rubric")
    if not rubric:
        return []
    from evals import judge

    criteria = rubric.get("criteria") or []
    threshold = float(rubric.get("threshold", 0.7))
    result = judge.score_rubric(text, criteria, model=rubric.get("model"))
    if result.error:
        return [f"rubric grader error: {result.error}"]
    if result.score < threshold:
        unmet = [c for c, ok in result.met.items() if not ok]
        return [f"rubric {result.score:.0%} < {threshold:.0%} (unmet: {unmet})"]
    return []


async def _run_workflow_case(client: AgentClient, case: dict) -> CaseResult:
    """Drive a workflow recipe end-to-end via ``POST /api/workflows/{name}/run``
    and assert on its synthesized output (patterns + optional rubric).

    Case schema: ``workflow`` (recipe name), ``inputs`` (dict),
    ``expected_patterns`` / ``verify_rubric`` against the workflow output."""
    cid, cat, name = case["id"], case.get("category", "workflow"), case.get("name", case["id"])
    wf = case.get("workflow")
    if not wf:
        return CaseResult(cid, cat, name, False, "case missing 'workflow' name")
    import time as _time

    start = _time.time()
    try:
        out = await client.run_workflow(
            wf, case.get("inputs") or {}, timeout_s=case.get("timeout_s", 300),
        )
    except Exception as e:  # noqa: BLE001
        return CaseResult(cid, cat, name, False, f"workflow run failed: {e!r}")
    duration_ms = int((_time.time() - start) * 1000)

    text = out.get("output", "") if isinstance(out, dict) else str(out)
    if not text.strip():
        return CaseResult(cid, cat, name, False, "empty workflow output", duration_ms=duration_ms)

    problems: list[str] = []
    text_lower = text.lower()
    for pattern in case.get("expected_patterns") or []:
        if pattern.lower() not in text_lower:
            problems.append(f"missing pattern {pattern!r}")
    problems += _check_rubric(case, text)

    detail = "; ".join(problems) if problems else f"OK ({duration_ms}ms, {len(text)} chars)"
    return CaseResult(
        cid, cat, name, passed=not problems, detail=detail,
        duration_ms=duration_ms, raw={"output": text[:300]},
    )


# ── dispatch ────────────────────────────────────────────────────────────────


_RUNNERS = {
    "agent_card": _run_agent_card,
    "auth_check": _run_auth_check,
    "ask": _run_ask,
    "stream": _run_stream,
    "goal": _run_goal_case,
    "workflow": _run_workflow_case,
}


async def run_one(client: AgentClient, case: dict) -> CaseResult:
    runner = _RUNNERS.get(case.get("kind", "ask"))
    if runner is None:
        return CaseResult(
            case["id"], case.get("category", "?"), case.get("name", "?"),
            False, f"unknown kind: {case.get('kind')}",
        )
    try:
        return await runner(client, case)
    except Exception as e:
        return CaseResult(
            case["id"], case.get("category", "?"), case.get("name", "?"),
            False, f"exception: {e!r}",
        )


# ── main ────────────────────────────────────────────────────────────────────


def _print_board(results: list[CaseResult]) -> None:
    width_id = max(len(r.id) for r in results)
    width_cat = max(len(r.category) for r in results)
    print()
    print(f"{'ID'.ljust(width_id)}  {'CAT'.ljust(width_cat)}  RESULT  TIME    TOKENS  DETAIL")
    print("-" * 90)
    pass_count = 0
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        if r.passed:
            pass_count += 1
        time_s = f"{r.duration_ms}ms".rjust(6)
        tokens = str(r.tokens).rjust(6) if r.tokens else "  -   "
        print(
            f"{r.id.ljust(width_id)}  {r.category.ljust(width_cat)}  "
            f"{mark}    {time_s}  {tokens}  {r.detail[:80]}"
        )
    print("-" * 90)
    print(f"\n{pass_count}/{len(results)} passed")


def _save_report(results: list[CaseResult], path: Path, *, model: str = "", base_url: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        # The model under test — tagged so reports are comparable across model
        # swaps (evals.compare / evals.report key off this). Auto-detected from
        # /healthz, overridable with --model-label.
        "model": model,
        "base_url": base_url,
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2))
    print(f"\nReport: {path}")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=None)
    p.add_argument("--tasks", default=None, help="comma-separated case IDs")
    p.add_argument("--category", default=None)
    p.add_argument("--out", default=None)
    p.add_argument(
        "--model-label", default=None,
        help="tag the report with this model name (default: auto-detect from /healthz)",
    )
    args = p.parse_args()

    tasks_path = Path(__file__).parent / "tasks.json"
    cases = json.loads(tasks_path.read_text())

    if args.tasks:
        wanted = set(args.tasks.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]

    if not cases:
        print("no cases match filters", file=sys.stderr)
        return 2

    client = AgentClient(base_url=args.base_url)

    # Tag the report with the model under test. --model-label wins; otherwise
    # ask /healthz what the running agent is serving so swaps are traceable.
    model_label = args.model_label or ""
    if not model_label:
        try:
            model_label = (await client.health()).get("model") or ""
        except Exception:
            model_label = ""

    print(f"Running {len(cases)} case(s) against {client.base_url}" + (f" [model: {model_label}]" if model_label else ""))
    results: list[CaseResult] = []
    for case in cases:
        sys.stdout.write(f"  {case['id']}... ")
        sys.stdout.flush()
        result = await run_one(client, case)
        sys.stdout.write(f"{'PASS' if result.passed else 'FAIL'}  {result.detail[:60]}\n")
        results.append(result)

    _print_board(results)

    out_path = Path(args.out) if args.out else (
        Path(__file__).parent / "results" / f"run-{int(time.time())}.json"
    )
    _save_report(results, out_path, model=model_label, base_url=client.base_url)

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
