"""portfolio — the PM / program orchestration layer (ADR 0055 P1).

One agent orchestrates work across MANY team-agents, each running its own project
board for its own repo (scale-out). This is **pure composition** of three existing
subsystems — no new dispatch or registry machinery:

  - fleet (`graph.fleet.supervisor`) — the team-agent registry (remote members)
  - delegates (`plugins.delegates`) — the A2A dispatch primitive (`A2aAdapter`)
  - project_board (its data router) — the structured remote board read

The PM treats each remote fleet member as a *board* addressed by its name: list
them (`portfolio_boards`), dispatch a feature to one over A2A (`portfolio_dispatch`),
read one back structured (`portfolio_board_read`), see a bounded cross-board rollup
(`portfolio_rollup`), and watch for changes without polling (`portfolio_watch` +
`portfolio_diff`). See ADR 0055.

P2 deltas are PULL-DIFF, not push: the PM snapshots each board (state per feature)
and reports what changed since the last check. A2A push notifications are task-scoped
(wrong granularity), the event bus is in-process, and a team-agent doesn't know its
PM — so a PM-side snapshot+diff, run on a schedule, is the thin correct shape (ADR
0055 P2; the optional inbox-push upgrade is deferred).
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.tools import tool


def register(registry) -> None:
    for t in _tools():
        registry.register_tool(t)


def _remote_by_name(name: str) -> dict | None:
    """The remote-member record (token INCLUDED) for a board name/id, or None.

    A board is addressed by the remote fleet member's name (ADR 0055 §2). Uses
    ``list_remotes()`` (not ``status()``) because dispatch + read need the stored
    bearer, which ``status()`` strips.
    """
    from graph.fleet import supervisor

    name = (name or "").strip()
    if not name:
        return None
    for rec in supervisor.list_remotes():
        if rec.get("name") == name or rec.get("id") == name:
            return rec
    return None


class _BoardUnavailable(Exception):
    """A team board couldn't be read (policy block, 404, HTTP error, network)."""


async def _fetch_board_features(rec: dict, state: str = "") -> list:
    """GET a remote team board's features (structured) — the shared read used by both
    the raw read and the rollup. Raises ``_BoardUnavailable`` so callers format their
    own message. The stored remote bearer authenticates both ``/a2a`` and the board
    API; the remote was egress-vetted at add_remote, re-checked here for parity with
    the A2A dispatch path."""
    url = rec["url"].rstrip("/") + "/api/plugins/project_board/features"
    from security import policy

    blocked = policy.check_url(url)
    if blocked:
        raise _BoardUnavailable(blocked)

    import httpx

    headers = {"Authorization": f"Bearer {rec['token']}"} if rec.get("token") else {}
    params = {"state": state} if state else None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers, params=params)
    except Exception as exc:  # noqa: BLE001
        raise _BoardUnavailable(str(exc)) from exc
    if r.status_code == 404:
        raise _BoardUnavailable("no project board exposed (project_board not enabled there)")
    if r.status_code >= 400:
        raise _BoardUnavailable(f"HTTP {r.status_code} {r.text[:200]}")
    return r.json().get("features", [])


def _rollup_one(name: str, features: list) -> dict:
    """Project a board's features into a BOUNDED rollup — lane counts + only the
    blocked / foundation (critical-path) items, never the full feature list. This is
    what keeps a PM's context small when reasoning over many boards."""
    counts: dict[str, int] = {}
    blocked: list[dict] = []
    critical: list[dict] = []
    for f in features:
        st = f.get("board_state", "backlog")
        counts[st] = counts.get(st, 0) + 1
        if f.get("blocked") or f.get("dag_blocked"):
            blocked.append({"id": f.get("id"), "title": f.get("title", "")})
        if f.get("foundation") and st != "done":
            critical.append({"id": f.get("id"), "title": f.get("title", ""), "state": st})
    return {"board": name, "total": len(features), "counts": counts, "blocked": blocked, "critical_path": critical}


def _parse_boards(boards: str) -> set | None:
    """Comma-separated board filter → a set of names, or None for all."""
    return {b.strip() for b in boards.split(",") if b.strip()} if boards else None


# ── P2 deltas: snapshot + diff (pull-diff, PM-side) ──────────────────────────────


def _snapshot_path():
    """Per-instance baseline for delta detection — scoped under the PM's data root so
    co-located instances don't collide (ADR 0004), mirroring remotes.json."""
    from infra.paths import data_home, scope_leaf

    return scope_leaf(data_home() / "portfolio_snapshot.json")


def _load_snapshot() -> dict:
    p = _snapshot_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001 — a corrupt snapshot just re-baselines, never breaks the tool
        return {}


def _save_snapshot(snap: dict) -> None:
    p = _snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap))


def _index_features(features: list) -> dict:
    """feature_id → the fields a diff cares about (state + blocked + title)."""
    return {
        f["id"]: {
            "state": f.get("board_state"),
            "blocked": bool(f.get("blocked") or f.get("dag_blocked")),
            "title": f.get("title", ""),
        }
        for f in features
        if f.get("id")
    }


def _diff_boards(prev: dict, curr: dict) -> dict:
    """Compare two feature-index snapshots → only the meaningful transitions: a feature
    reaching ``done`` (PR merged), newly blocked, unblocked, or newly appearing."""
    merged, newly_blocked, unblocked, new = [], [], [], []
    for fid, c in curr.items():
        p = prev.get(fid)
        if p is None:
            new.append({"id": fid, "title": c["title"], "state": c["state"]})
            continue
        if c["state"] == "done" and p.get("state") != "done":
            merged.append({"id": fid, "title": c["title"]})
        if c["blocked"] and not p.get("blocked"):
            newly_blocked.append({"id": fid, "title": c["title"]})
        elif p.get("blocked") and not c["blocked"]:
            unblocked.append({"id": fid, "title": c["title"]})
    out = {}
    if merged:
        out["merged"] = merged
    if newly_blocked:
        out["newly_blocked"] = newly_blocked
    if unblocked:
        out["unblocked"] = unblocked
    if new:
        out["new"] = new
    return out


async def _compute_portfolio_diff(wanted: set | None) -> dict:
    """Fan out across the (filtered) team boards, diff each against the saved baseline,
    and rewrite the baseline. Returns ``{recs, first_run, changes}``. On the first run
    (no baseline) it records the baseline and reports nothing — there's no 'before'."""
    from graph.fleet import supervisor

    recs = [
        r
        for r in supervisor.list_remotes()
        if wanted is None or r.get("name") in wanted or r.get("id") in wanted
    ]
    snap = _load_snapshot()
    first_run = not snap

    async def _one(rec: dict):
        name = rec.get("name")
        try:
            feats = await _fetch_board_features(rec)
        except _BoardUnavailable as exc:
            return name, {"error": str(exc)}, None
        idx = _index_features(feats)
        return name, _diff_boards(snap.get(name, {}), idx), idx

    results = await asyncio.gather(*[_one(r) for r in recs]) if recs else []
    changes = {}
    for name, deltas, idx in results:
        if idx is not None:  # only advance the baseline for boards we actually read
            snap[name] = idx
        if deltas and not first_run:  # first run = pure baseline, suppress the all-new noise
            changes[name] = deltas
    _save_snapshot(snap)
    return {"recs": len(recs), "first_run": first_run, "changes": changes}


def _dispatch_instruction(title: str, spec: str, acceptance_criteria: str, files_to_modify: str) -> str:
    lines = [
        "You manage a project board (the project_board plugin). Create a new feature on it "
        "and mark it ready so your spawn loop picks it up — use board_create_feature then "
        "board_mark_ready.",
        f"Title: {title}",
        f"Spec: {spec}",
    ]
    if acceptance_criteria:
        lines.append(f"Acceptance criteria: {acceptance_criteria}")
    if files_to_modify:
        lines.append(f"Files to modify: {files_to_modify}")
    lines.append("Report the created feature id and its board state.")
    return "\n".join(lines)


def _tools() -> list:
    @tool
    def portfolio_boards() -> str:
        """List the team boards you can orchestrate. Each is a remote team-agent — its
        own protoAgent instance running a project board for its repo. Returns each
        board's name, url, and whether it's reachable. Use the name as the ``board``
        argument to portfolio_dispatch / portfolio_board_read."""
        from graph.fleet import supervisor

        boards = [
            {"board": a["name"], "url": a.get("url"), "reachable": bool(a.get("running"))}
            for a in supervisor.status()
            if a.get("remote") and a.get("url")
        ]
        if not boards:
            return (
                "No team boards yet. A team board is a remote protoAgent (running the "
                "project_board plugin for its repo) registered as a fleet member — add one "
                "via the console (Discover → Add to this fleet) or POST /api/fleet/remotes."
            )
        return json.dumps(boards, indent=2)

    @tool
    async def portfolio_dispatch(
        board: str,
        title: str,
        spec: str,
        acceptance_criteria: str = "",
        files_to_modify: str = "",
    ) -> str:
        """Dispatch a feature to a team board over A2A. ``board`` is a team-agent name
        (see portfolio_boards). The team's lead agent creates the feature on its OWN
        board and marks it ready; its loop then ships the PR in ITS repo. Give a
        self-sufficient spec + acceptance criteria + the files to touch — a vague task
        makes a coder produce nothing. Returns the team agent's reply."""
        rec = _remote_by_name(board)
        if rec is None:
            return f"Error: no team board named {board!r}. Call portfolio_boards to list them."
        from plugins.delegates.adapters import ADAPTERS, Delegate

        d = Delegate(
            name=board,
            type="a2a",
            url=rec["url"].rstrip("/") + "/a2a",
            auth_scheme="bearer",
            auth_token=rec.get("token", ""),
        )
        try:
            return await ADAPTERS["a2a"].dispatch(
                d, _dispatch_instruction(title, spec, acceptance_criteria, files_to_modify), timeout=120
            )
        except Exception as exc:  # noqa: BLE001 — surface the dispatch failure to the model
            return f"Error dispatching to {board!r}: {exc}"

    @tool
    async def portfolio_board_read(board: str, state: str = "") -> str:
        """Read a team board's current state (structured) — the bounded view a PM
        reasons over. ``board`` is a team-agent name (see portfolio_boards); optional
        ``state`` filters to one lane (backlog/ready/in_progress/in_review/done/
        blocked). Returns the features as JSON."""
        rec = _remote_by_name(board)
        if rec is None:
            return f"Error: no team board named {board!r}. Call portfolio_boards to list them."
        try:
            feats = await _fetch_board_features(rec, state)
        except _BoardUnavailable as exc:
            return f"Error reading {board!r} board: {exc}"
        return json.dumps(feats, indent=2)

    @tool
    async def portfolio_rollup(boards: str = "") -> str:
        """A BOUNDED portfolio view across team boards: per-board lane counts + only the
        blocked / critical-path (foundation) items — NOT every feature — so you can
        reason over MANY boards at once without pulling each one raw. Optional comma-
        separated ``boards`` filters to specific team-agent names (default = all). An
        unreachable board is reported with an ``error`` instead of failing the rollup."""
        from graph.fleet import supervisor

        wanted = {b.strip() for b in boards.split(",") if b.strip()} if boards else None
        recs = [
            r
            for r in supervisor.list_remotes()
            if wanted is None or r.get("name") in wanted or r.get("id") in wanted
        ]
        if not recs:
            return (
                "No matching team boards. Call portfolio_boards to list them."
                if wanted
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )

        async def _one(rec: dict) -> dict:
            try:
                feats = await _fetch_board_features(rec)
            except _BoardUnavailable as exc:
                return {"board": rec.get("name"), "error": str(exc)}
            return _rollup_one(rec.get("name"), feats)

        rollups = await asyncio.gather(*[_one(r) for r in recs])
        return json.dumps(rollups, indent=2)

    @tool
    async def portfolio_diff(boards: str = "") -> str:
        """Report what CHANGED on the team boards since the last check — features that
        merged (reached done), newly blocked, unblocked, or newly added — then update
        the baseline. The bounded, push-free way to stay current: schedule this (see
        portfolio_watch) and each run surfaces only the deltas. The FIRST run records a
        baseline and reports nothing (there's no 'before'). Optional comma-separated
        ``boards`` filter."""
        res = await _compute_portfolio_diff(_parse_boards(boards))
        if res["recs"] == 0:
            return (
                "No matching team boards. Call portfolio_boards to list them."
                if boards
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )
        if res["first_run"]:
            return (
                f"Baseline recorded for {res['recs']} board(s). Future portfolio_diff calls "
                "report only what changed since now."
            )
        if not res["changes"]:
            return "No board changes since the last check."
        return json.dumps(res["changes"], indent=2)

    @tool
    async def portfolio_watch(interval_min: int = 15, boards: str = "") -> str:
        """Start watching the team boards for changes WITHOUT polling: record a baseline
        now, then hand you the exact schedule_task call to run a recurring portfolio_diff.
        Each scheduled fire arrives as a turn carrying only the changes since the prior
        sweep — so the system polls for you, not your reasoning loop. Optional
        ``interval_min`` (default 15) and comma-separated ``boards`` filter."""
        res = await _compute_portfolio_diff(_parse_boards(boards))
        if res["recs"] == 0:
            return (
                "No matching team boards to watch."
                if boards
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )
        interval = max(1, int(interval_min))
        cron = f"*/{interval} * * * *" if interval < 60 else "0 * * * *"
        filt = f' boards="{boards}"' if boards else ""
        return (
            f"Baseline captured for {res['recs']} board(s). To receive deltas without polling, "
            "schedule a recurring sweep with your schedule_task tool:\n\n"
            f'  schedule_task(prompt="Run portfolio_diff{filt} and report any board changes; '
            f'if there are none, do nothing.", when="{cron}")\n\n'
            "Each fire arrives as a turn carrying only the changes since the prior sweep."
        )

    return [
        portfolio_boards,
        portfolio_dispatch,
        portfolio_board_read,
        portfolio_rollup,
        portfolio_diff,
        portfolio_watch,
    ]
