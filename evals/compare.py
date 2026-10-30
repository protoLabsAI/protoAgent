"""Compare two eval runs into a markdown diff report.

Reads two reports written by ``evals.runner`` (``evals/results/run-*.json``,
shape ``{"total", "passed", "results": [{"id","category","name","passed",
"detail"}]}``) and reports the pass-rate delta, per-category deltas, and which
cases flipped — the regression check for a model/backend/prompt change.

Backported from the protoLabs fleet (protoResearcher ``evals/compare.py``).

    python -m evals.compare evals/results/run-OLD.json evals/results/run-NEW.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# ADR 0126 D1 — eval tiers. ``gate`` is deterministic conformity ("this still
# works") and fails the nightly run; ``tracked`` is LLM-judge frequency ("how
# well, how often") and only ever moves a trend. Gate is the headline number,
# so it is always reported first.
_VALID_TIERS = ("gate", "tracked")
_TIER_ORDER = {t: i for i, t in enumerate(_VALID_TIERS)}

# ADR 0126 D5 — per-instance eval cases. A case loaded from the instance's
# ``<config dir>/evals/cases.json`` has its id namespaced with this prefix so it
# can never collide with a core ``tasks.json`` id, and so a saved report row (or a
# loaded case) is recognisable as an instance case by its id alone.
INSTANCE_ID_PREFIX = "instance:"


def is_instance_case(item: dict) -> bool:
    """True for a per-instance case or report row (ADR 0126 D5).

    Detected by the namespaced ``instance:`` id so it works on a saved report row
    exactly as on a freshly loaded case; the explicit ``instance`` flag the runner
    stamps is a secondary signal (a legacy row predating that flag still resolves
    via its id)."""
    if item.get("instance"):
        return True
    return str(item.get("id", "")).startswith(INSTANCE_ID_PREFIX)


def resolve_tier(item: dict) -> str:
    """Resolve the eval tier of a case or a report row (ADR 0126 D1).

    An explicit ``tier`` wins and must be one of ``gate``/``tracked`` — an
    unknown value is a load-time error naming the case so a typo can't silently
    drop a case out of the gate. Otherwise a case/row carrying a ``verify_rubric``
    defaults to ``tracked`` (graded by the LLM judge, trend only) and everything
    else to ``gate`` (deterministic, counts against the nightly gate).

    The same resolver reads cases (which carry ``verify_rubric``) and historical
    report rows (which carry a resolved ``tier``, or neither when they predate
    this field — in which case they fall through to ``gate``)."""
    tier = item.get("tier")
    if tier is not None:
        if tier not in _VALID_TIERS:
            raise ValueError(
                f"case {item.get('id', '?')!r}: unknown tier {tier!r} "
                f"(expected one of {', '.join(_VALID_TIERS)})"
            )
        return tier
    return "tracked" if item.get("verify_rubric") else "gate"


def _tier_order(tier: str) -> tuple[int, str]:
    """Sort key putting ``gate`` before ``tracked`` before any other tier."""
    return (_TIER_ORDER.get(tier, len(_VALID_TIERS)), tier)


def _by_id(report: dict) -> dict[str, bool]:
    return {r["id"]: bool(r["passed"]) for r in report.get("results", [])}


def _tier_passed(report: dict) -> dict[str, tuple[int, int]]:
    """tier -> (passed, total), recomputed from rows (skipped rows excluded).

    Each row's tier is resolved with :func:`resolve_tier`, so legacy reports
    whose rows predate the ``tier`` field still split (defaulting to ``gate``)."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in report.get("results", []):
        if r.get("skipped"):
            continue
        cell = agg[resolve_tier(r)]
        cell[1] += 1
        if r.get("passed"):
            cell[0] += 1
    return {t: (p, tot) for t, (p, tot) in agg.items()}


def _tiers_of(report: dict) -> dict[str, tuple[int, int]]:
    """tier -> (passed, total). Prefers the report's own ``tiers`` block (written
    by the runner since ADR 0126 E1b); falls back to recomputing from rows so a
    legacy report with no tiers block / no per-row tier still renders a split."""
    block = report.get("tiers")
    if isinstance(block, dict) and block:
        out: dict[str, tuple[int, int]] = {}
        for tier, agg in block.items():
            if isinstance(agg, dict):
                out[tier] = (int(agg.get("passed", 0)), int(agg.get("total", 0)))
        if out:
            return out
    return _tier_passed(report)


def _category_passed(report: dict) -> dict[str, tuple[int, int]]:
    """category -> (passed, total)."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in report.get("results", []):
        cell = agg[r.get("category", "?")]
        cell[1] += 1
        if r.get("passed"):
            cell[0] += 1
    return {c: (p, t) for c, (p, t) in agg.items()}


def _pct(passed: int, total: int) -> str:
    return f"{(100 * passed / total):.0f}%" if total else "—"


def _rate(passed: int, total: int) -> float:
    """Pass rate as a float in [0, 1]; 0.0 for an empty tier (so a delta is defined)."""
    return passed / total if total else 0.0


def compare_reports(old: dict, new: dict) -> str:
    """Return a markdown comparison of two eval reports."""
    o_total, o_pass = old.get("total", 0), old.get("passed", 0)
    n_total, n_pass = new.get("total", 0), new.get("passed", 0)
    delta = n_pass - o_pass

    lines = ["# Eval comparison", ""]
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
    lines.append(
        f"**Overall:** {o_pass}/{o_total} ({_pct(o_pass, o_total)}) → "
        f"{n_pass}/{n_total} ({_pct(n_pass, n_total)})  {arrow} {delta:+d}"
    )
    lines.append("")

    # Per-tier — gate first, since the gate pass rate is the number that gates
    # the nightly run (ADR 0126 D1). Regressions and improvements are reported
    # per tier so a tracked-tier judge wobble is never read as a gate break.
    ot, nt_ = _tiers_of(old), _tiers_of(new)
    tiers = sorted(set(ot) | set(nt_), key=_tier_order)
    lines.append("| Tier | Old | New | Δ |")
    lines.append("|---|---|---|---|")
    for tier in tiers:
        op, otot = ot.get(tier, (0, 0))
        nps, ntot = nt_.get(tier, (0, 0))
        label = f"**{tier}**" if tier == "gate" else tier
        lines.append(f"| {label} | {op}/{otot} | {nps}/{ntot} | {nps - op:+d} |")
    lines.append("")

    # Per-category
    oc, nc = _category_passed(old), _category_passed(new)
    cats = sorted(set(oc) | set(nc))
    lines.append("| Category | Old | New | Δ |")
    lines.append("|---|---|---|---|")
    for c in cats:
        op, ot = oc.get(c, (0, 0))
        nps, nt = nc.get(c, (0, 0))
        lines.append(f"| {c} | {op}/{ot} | {nps}/{nt} | {nps - op:+d} |")
    lines.append("")

    # Flips
    o_ids, n_ids = _by_id(old), _by_id(new)
    regressed = sorted(i for i in n_ids if o_ids.get(i) and not n_ids[i])
    fixed = sorted(i for i in n_ids if n_ids[i] and o_ids.get(i) is False)
    if regressed:
        lines.append("### ❌ Newly failing")
        lines += [f"- `{i}`" for i in regressed]
        lines.append("")
    if fixed:
        lines.append("### ✅ Newly passing")
        lines += [f"- `{i}`" for i in fixed]
        lines.append("")
    if not regressed and not fixed:
        lines.append("_No cases flipped._")
    return "\n".join(lines)


# ── learned-context A/B (ADR 0126 D6) ────────────────────────────────────────
#
# ``scripts/evals_ab.py`` runs the same suite twice — arm A on the bundled
# template only, arm B on the template PLUS a copy of a source agent's learned
# memories and private skills — and these functions diff the two arm reports.
# Everything below reads "B minus A": a positive gate delta means the learned
# context earned its keep; a *negative* one means it hurt (stale or poisoned
# memory), which the bad-memory fixture exists to prove is detectable.


def _arm_summary(report: dict) -> dict:
    """Per-arm roll-up for the A/B comparison: overall + per-tier pass counts, the
    turn count, and cost (latency + tokens).

    ``turns`` is the number of cases that actually ran (skipped cases carry no
    pass/fail or cost signal, so they are excluded — same convention as the tier
    split). Latency and tokens are summed over the arm's rows and averaged over the
    rows that recorded one, so an ``agent_card``/``auth_check`` row with no timing
    never drags the average to zero."""
    rows = [r for r in report.get("results", []) if not r.get("skipped")]
    timed = [r for r in rows if r.get("duration_ms")]
    tokened = [r for r in rows if r.get("tokens")]
    total_ms = sum(int(r.get("duration_ms") or 0) for r in rows)
    total_tokens = sum(int(r.get("tokens") or 0) for r in rows)
    tiers = _tiers_of(report)
    return {
        "total": report.get("total", len(rows)),
        "passed": report.get("passed", sum(1 for r in rows if r.get("passed"))),
        "turns": len(rows),
        "latency_ms_total": total_ms,
        "latency_ms_avg": round(total_ms / len(timed)) if timed else 0,
        "tokens_total": total_tokens,
        "tokens_avg": round(total_tokens / len(tokened)) if tokened else 0,
        "tiers": {t: [p, tot] for t, (p, tot) in tiers.items()},
    }


def compare_ab(report_a: dict, report_b: dict) -> dict:
    """Structured comparison of the baseline arm (A) and the learned arm (B).

    Returns a JSON-serialisable dict carrying, all B-minus-A (ADR 0126 D6):

    * ``arms`` — per-arm overall/tier pass counts, turn count, latency and tokens;
    * ``tier_deltas`` — per-tier ``(passed, total)`` for each arm plus the pass-rate
      delta, gate first (the headline, ADR 0126 D1);
    * ``headline`` — the gate pass-count and pass-rate delta on its own;
    * ``case_deltas`` — every case id with its A/B pass flags and the per-case
      delta (each arm runs a case once, so the delta is ``-1`` / ``0`` / ``+1``, or
      ``None`` when the case ran in only one arm);
    * ``flips`` (and the ``regressions`` / ``improvements`` partition) — every case
      that flipped between arms, each carrying the ``trace_id`` of BOTH runs so a
      reviewer can open the two traces side by side."""
    a_tiers, b_tiers = _tiers_of(report_a), _tiers_of(report_b)
    tier_deltas: dict[str, dict] = {}
    for tier in sorted(set(a_tiers) | set(b_tiers), key=_tier_order):
        ap, at = a_tiers.get(tier, (0, 0))
        bp, bt = b_tiers.get(tier, (0, 0))
        tier_deltas[tier] = {
            "a": [ap, at],
            "b": [bp, bt],
            "delta_passed": bp - ap,
            "delta_rate": round(_rate(bp, bt) - _rate(ap, at), 4),
        }

    a_ids, b_ids = _by_id(report_a), _by_id(report_b)
    a_rows = {r["id"]: r for r in report_a.get("results", []) if "id" in r}
    b_rows = {r["id"]: r for r in report_b.get("results", []) if "id" in r}

    case_deltas: list[dict] = []
    flips: list[dict] = []
    for cid in sorted(set(a_ids) | set(b_ids)):
        a_pass = a_ids.get(cid)
        b_pass = b_ids.get(cid)
        both = a_pass is not None and b_pass is not None
        case_deltas.append(
            {
                "id": cid,
                "a_passed": a_pass,
                "b_passed": b_pass,
                "delta": (int(bool(b_pass)) - int(bool(a_pass))) if both else None,
            }
        )
        if not both or a_pass == b_pass:
            continue
        # A flip: the case ran in both arms and the verdict differs. Carry the trace
        # id of each run so the markdown can link both — the whole point of the A/B
        # is being able to open "why it passed" next to "why it broke".
        row = b_rows.get(cid) or a_rows.get(cid) or {}
        flips.append(
            {
                "id": cid,
                "name": row.get("name", cid),
                "tier": resolve_tier(row),
                "direction": "regression" if (a_pass and not b_pass) else "improvement",
                "a_trace": str((a_rows.get(cid) or {}).get("trace_id", "")),
                "b_trace": str((b_rows.get(cid) or {}).get("trace_id", "")),
            }
        )

    gate = tier_deltas.get("gate", {"delta_passed": 0, "delta_rate": 0.0, "a": [0, 0], "b": [0, 0]})
    return {
        "kind": "ab-compare",
        "model": report_b.get("model") or report_a.get("model") or "",
        "arms": {"A": _arm_summary(report_a), "B": _arm_summary(report_b)},
        "headline": {"gate_delta_passed": gate["delta_passed"], "gate_delta_rate": gate["delta_rate"]},
        "tier_deltas": tier_deltas,
        "case_deltas": case_deltas,
        "flips": flips,
        "regressions": [f for f in flips if f["direction"] == "regression"],
        "improvements": [f for f in flips if f["direction"] == "improvement"],
    }


def _trace_link(trace_id: str) -> str:
    """A Langfuse trace URL for ``trace_id`` when tracing is live, else the raw id.

    Reuses the console's own resolver so the link shape matches what the friction
    and telemetry views render (the same helper ``scripts/evals_nightly.py`` uses);
    imported lazily so a plain ``evals.compare`` import never pulls ``operator_api``
    and a tracing blip never breaks the report."""
    trace_id = (trace_id or "").strip()
    if not trace_id:
        return ""
    try:
        from urllib.parse import quote

        from operator_api.telemetry_routes import _resolve_trace_url_template

        template = _resolve_trace_url_template()
    except Exception:  # noqa: BLE001 — a tracing blip must not stop the report
        template = None
    if template:
        return template.replace("{trace_id}", quote(trace_id, safe=""))
    return trace_id


def _trace_cell(trace_id: str, trace_link) -> str:
    """One flip-table trace cell: a markdown link when the resolver returns a URL, a
    bare code span for a raw id with no Langfuse template, ``—`` when the run had no
    trace id at all."""
    tid = (trace_id or "").strip()
    if not tid:
        return "—"
    link = trace_link(tid)
    return f"[`{tid}`]({link})" if link and link != tid else f"`{tid}`"


def _delta_rate_str(delta_rate: float) -> str:
    """``delta_rate`` (a fraction) as signed percentage points: ``+20%`` / ``-20%`` / ``±0%``."""
    pts = round(delta_rate * 100)
    return "±0%" if pts == 0 else f"{pts:+d}%"


def format_ab_markdown(comparison: dict, *, a_file: str = "", b_file: str = "", trace_link=None) -> str:
    """Render :func:`compare_ab` output as the human-readable ``ab-compare-*.md``.

    ``a_file`` / ``b_file`` name the two arm reports this was built from (so the md
    points back at them). ``trace_link`` resolves a trace id to a URL and defaults to
    :func:`_trace_link`; it is injectable so a test can render links without a live
    Langfuse."""
    link = trace_link or _trace_link
    arms = comparison["arms"]
    tier_deltas = comparison["tier_deltas"]

    lines = ["# Eval A/B — learned context (B) vs baseline (A)", ""]
    model = comparison.get("model")
    if model:
        lines.append(f"**Model:** `{model}`")
        lines.append("")

    # Headline: the gate delta on its own. Gate is the number that gates the nightly
    # run (ADR 0126 D1), so it leads — a negative gate delta is the harm signal.
    gate = tier_deltas.get("gate")
    if gate:
        ap, at = gate["a"]
        bp, bt = gate["b"]
        arrow = "▲" if gate["delta_passed"] > 0 else ("▼" if gate["delta_passed"] < 0 else "■")
        lines.append(
            f"**Headline — gate:** A {ap}/{at} ({_pct(ap, at)}) → B {bp}/{bt} ({_pct(bp, bt)})  "
            f"{arrow} {gate['delta_passed']:+d} ({_delta_rate_str(gate['delta_rate'])})"
        )
        lines.append("")
    lines.append(
        "Arm A is the bundled template only; arm B adds a copy of the source agent's learned "
        "memories and private skills. Everything below is **B minus A** — a *negative* gate delta "
        "means the learned context hurt (stale or poisoned memory), not helped."
    )
    lines.append("")

    # Per-tier pass rate, gate first.
    lines.append("## Per-tier pass rate")
    lines.append("")
    lines.append("| Tier | A | B | Δ |")
    lines.append("|---|---|---|---|")
    for tier in sorted(tier_deltas, key=_tier_order):
        d = tier_deltas[tier]
        ap, at = d["a"]
        bp, bt = d["b"]
        label = f"**{tier}**" if tier == "gate" else tier
        lines.append(
            f"| {label} | {ap}/{at} ({_pct(ap, at)}) | {bp}/{bt} ({_pct(bp, bt)}) | "
            f"{d['delta_passed']:+d} ({_delta_rate_str(d['delta_rate'])}) |"
        )
    lines.append("")

    # Cost per arm — turns, latency, tokens.
    lines.append("## Cost per arm")
    lines.append("")
    lines.append("| Arm | Cases (turns) | Latency total | Latency avg | Tokens total | Tokens avg |")
    lines.append("|---|---|---|---|---|---|")
    for key, name in (("A", "A (baseline)"), ("B", "B (learned)")):
        s = arms[key]
        lines.append(
            f"| {name} | {s['turns']} | {s['latency_ms_total']}ms | {s['latency_ms_avg']}ms | "
            f"{s['tokens_total'] or '—'} | {s['tokens_avg'] or '—'} |"
        )
    lines.append("")

    # Flips — regressions first (the harm), each with BOTH trace links.
    regressions = comparison["regressions"]
    improvements = comparison["improvements"]
    lines.append("## Flips")
    lines.append("")
    if not regressions and not improvements:
        lines.append("_No cases flipped between arms._")
        lines.append("")
    else:
        for heading, group, blurb in (
            ("### ❌ Regressions (A pass → B fail)", regressions, "the learned context broke these"),
            ("### ✅ Improvements (A fail → B pass)", improvements, "the learned context fixed these"),
        ):
            if not group:
                continue
            lines.append(f"{heading} — {blurb}")
            lines.append("")
            lines.append("| Case | Tier | Trace A | Trace B |")
            lines.append("|---|---|---|---|")
            for f in group:
                lines.append(
                    f"| `{f['id']}` | {f['tier']} | {_trace_cell(f['a_trace'], link)} | "
                    f"{_trace_cell(f['b_trace'], link)} |"
                )
            lines.append("")

    if a_file or b_file:
        lines.append(f"_Arms: A = `{a_file or '?'}`, B = `{b_file or '?'}`._")
    return "\n".join(lines).rstrip() + "\n"


def write_ab_comparison(
    report_a: dict,
    report_b: dict,
    *,
    out_dir: Path,
    ts: int | str,
    a_file: str = "",
    b_file: str = "",
) -> tuple[Path, Path]:
    """Write ``ab-compare-<ts>.json`` + ``ab-compare-<ts>.md`` into ``out_dir`` and
    return both paths. Called by ``scripts/evals_ab.py`` once both arm reports exist."""
    out_dir.mkdir(parents=True, exist_ok=True)
    comparison = compare_ab(report_a, report_b)
    json_path = out_dir / f"ab-compare-{ts}.json"
    md_path = out_dir / f"ab-compare-{ts}.md"
    json_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    md_path.write_text(format_ab_markdown(comparison, a_file=a_file, b_file=b_file), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        sys.stderr.write("usage: python -m evals.compare <old.json> <new.json>\n")
        return 2
    old = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    new = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(compare_reports(old, new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
