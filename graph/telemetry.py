"""Plugin telemetry + agent decision log — the observability surface an unattended or
agentic plugin needs: a capped audit trail of what the agent DID (and why), a standard
telemetry envelope, and a themed HTML panel any plugin view can drop in.

The lesson from the SpaceTraders two-loop fleet: an autonomous agent steering a deterministic
engine is only trustworthy if you can SEE what it changed and how it's doing. That plugin
hand-rolled a decision-log ring buffer (`_DECISIONS` / `_log_decision`), a structured
``st_report`` (status + metrics + hints + decisions), and a dashboard panel. This generalizes
all three:

  * :class:`DecisionLog` — record every agent decision (the audit trail).
  * :func:`telemetry`    — assemble the standard envelope (status / metrics / hints /
    decisions / sections) a report tool returns and a panel renders.
  * :func:`render_html`  — render that envelope as a self-contained, ``--pl-*``-token-themed
    HTML fragment to drop into a plugin's console view (carries its own styles + fallbacks,
    so it needs no specific stylesheet loaded).

Pure stdlib (+ ``html.escape``) — host-free, directly unit-tested.
"""

from __future__ import annotations

import html
from typing import Any

__all__ = ["DecisionLog", "telemetry", "render_html"]


class DecisionLog:
    """A capped, newest-last log of agent decisions — the audit trail for an autonomous
    plugin (what the agent changed, and optionally why). Surface ``entries()`` in a report
    tool and a console panel.

        log = DecisionLog()
        log.record("tune", "min_margin: 30 → 15")
        log.record("strategy", "→ trade-max", reason="cr/hr falling")
        log.entries(5)   # [{'action': 'strategy', 'detail': '→ trade-max', 'reason': …}, …]
    """

    def __init__(self, cap: int = 50):
        self._cap = max(1, cap)
        self._entries: list[dict] = []

    def record(self, action: str, detail: str = "", **fields: Any) -> dict:
        """Append a decision ``{action, detail, **fields}`` (e.g. a ``reason``/``ts``) and
        return it. Oldest entries fall off past the cap."""
        entry = {"action": str(action), "detail": str(detail), **fields}
        self._entries.append(entry)
        del self._entries[: -self._cap]
        return entry

    def entries(self, n: int | None = None) -> list[dict]:
        """The last ``n`` entries (newest last), or all of them."""
        return list(self._entries[-n:]) if n else list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _as_decisions(decisions: Any) -> list[dict]:
    if decisions is None:
        return []
    if isinstance(decisions, DecisionLog):
        return decisions.entries()
    return list(decisions)


def telemetry(*, status: str | None = None, metrics: dict | None = None,
              hints: list | None = None, decisions: Any = None,
              sections: list | None = None, **extra: Any) -> dict:
    """Assemble the standard telemetry envelope — the shape a plugin's report tool returns
    and :func:`render_html` (or a future console panel) renders uniformly:

        {
          "status":   "running · 1,240,000 cr · 18,400 cr/hr",   # one-line headline
          "metrics":  {"credits": 1_240_000, "cr/hr": 18_400},   # name -> value (stat cards)
          "hints":    ["idle capital — reinvest", …],            # deterministic nudges
          "decisions": [{"action": "tune", "detail": "min_margin 30→15"}, …],  # or a DecisionLog
          "sections": [{"title": "Fleet", "columns": ["ship","role"], "rows": [[...], ...]}],
          ...extra                                               # plugin-specific keys pass through
        }
    """
    return {
        "status": status or "",
        "metrics": dict(metrics or {}),
        "hints": list(hints or []),
        "decisions": _as_decisions(decisions),
        "sections": list(sections or []),
        **extra,
    }


# ── HTML panel ─────────────────────────────────────────────────────────────────────────
_STYLE = """
.pl-tele{font-family:var(--pl-font-sans,system-ui,sans-serif);color:var(--pl-color-fg,#e8e8e8);
  background:var(--pl-color-bg,#111);padding:var(--pl-space-4,16px);border-radius:var(--pl-radius,10px)}
.pl-tele h3{margin:0 0 4px;font-size:15px}
.pl-tele .pl-tele-status{color:var(--pl-color-fg-muted,#9a9a9a);font-size:13px;margin-bottom:12px}
.pl-tele-metrics{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}
.pl-tele-metric{background:var(--pl-color-bg-raised,#1b1b1b);border:1px solid var(--pl-color-border,#2a2a2a);
  border-radius:var(--pl-radius,8px);padding:8px 12px;min-width:90px}
.pl-tele-metric .v{font-size:18px;font-weight:600}
.pl-tele-metric .k{font-size:11px;color:var(--pl-color-fg-muted,#9a9a9a);text-transform:uppercase;letter-spacing:.04em}
.pl-tele table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0 12px}
.pl-tele th,.pl-tele td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--pl-color-border,#2a2a2a)}
.pl-tele th{color:var(--pl-color-fg-muted,#9a9a9a);font-weight:500}
.pl-tele .pl-tele-badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
  background:var(--pl-color-accent,#9b87f2);color:#fff}
.pl-tele ul{margin:0 0 12px;padding-left:18px}
.pl-tele li{font-size:13px;margin:2px 0}
.pl-tele h4{margin:10px 0 2px;font-size:12px;color:var(--pl-color-fg-muted,#9a9a9a);
  text-transform:uppercase;letter-spacing:.04em}
"""


def _esc(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    return html.escape("" if v is None else str(v))


def render_html(envelope: dict, *, title: str = "Telemetry") -> str:
    """Render a telemetry envelope as a self-contained, ``--pl-*``-token-themed HTML fragment
    (a ``<section class="pl-tele">`` carrying its own ``<style>`` with fallbacks), ready to
    drop into a plugin's console iframe view. All values are HTML-escaped.
    """
    env = envelope or {}
    parts = [f"<style>{_STYLE}</style>", '<section class="pl-tele">',
             f"<h3>{_esc(title)}</h3>"]
    if env.get("status"):
        parts.append(f'<div class="pl-tele-status">{_esc(env["status"])}</div>')

    metrics = env.get("metrics") or {}
    if metrics:
        cards = "".join(
            f'<div class="pl-tele-metric"><div class="v">{_esc(v)}</div>'
            f'<div class="k">{_esc(k)}</div></div>' for k, v in metrics.items())
        parts.append(f'<div class="pl-tele-metrics">{cards}</div>')

    decisions = env.get("decisions") or []
    if decisions:
        rows = "".join(
            f'<tr><td><span class="pl-tele-badge">{_esc(d.get("action", ""))}</span></td>'
            f'<td>{_esc(d.get("detail", ""))}</td></tr>' for d in reversed(decisions))
        parts.append('<h4>Decisions</h4><table><tr><th>Move</th><th>Detail</th></tr>'
                     f'{rows}</table>')

    for sec in (env.get("sections") or []):
        cols = sec.get("columns") or []
        head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
                       for row in (sec.get("rows") or []))
        parts.append(f'<h4>{_esc(sec.get("title", ""))}</h4>'
                     f"<table><tr>{head}</tr>{body}</table>")

    hints = env.get("hints") or []
    if hints:
        items = "".join(f"<li>{_esc(h)}</li>" for h in hints)
        parts.append(f"<h4>Hints</h4><ul>{items}</ul>")

    parts.append("</section>")
    return "".join(parts)
