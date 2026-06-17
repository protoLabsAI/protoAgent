"""portfolio — the PM / program orchestration layer (ADR 0055 P1).

One agent orchestrates work across MANY team-agents, each running its own project
board for its own repo (scale-out). This is **pure composition** of three existing
subsystems — no new dispatch or registry machinery:

  - fleet (`graph.fleet.supervisor`) — the team-agent registry (remote members)
  - delegates (`plugins.delegates`) — the A2A dispatch primitive (`A2aAdapter`)
  - project_board (its data router) — the structured remote board read

The PM treats each remote fleet member as a *board* addressed by its name: list
them (`portfolio_boards`), dispatch a feature to one over A2A (`portfolio_dispatch`),
and read one back structured (`portfolio_board_read`). See ADR 0055.
"""

from __future__ import annotations

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

        url = rec["url"].rstrip("/") + "/api/plugins/project_board/features"
        # The remote was egress-vetted at add_remote; re-check the read URL too (parity
        # with the A2A dispatch path, which guards every call).
        from security import policy

        blocked = policy.check_url(url)
        if blocked:
            return f"Error reading {board!r} board: {blocked}"

        import httpx

        headers = {"Authorization": f"Bearer {rec['token']}"} if rec.get("token") else {}
        params = {"state": state} if state else None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, headers=headers, params=params)
        except Exception as exc:  # noqa: BLE001
            return f"Error reading {board!r} board: {exc}"
        if r.status_code == 404:
            return f"{board!r} doesn't expose a project board (project_board not enabled there)."
        if r.status_code >= 400:
            return f"Error reading {board!r} board: HTTP {r.status_code} {r.text[:200]}"
        return json.dumps(r.json().get("features", []), indent=2)

    return [portfolio_boards, portfolio_dispatch, portfolio_board_read]
