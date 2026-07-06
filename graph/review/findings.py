"""The findings convention for adversarial code review (ADR 0077).

One place defines what a review "finding" is; everything else refers here:

- The `code-review` workflow's prompts embed :data:`FINDINGS_CONTRACT` so every
  finder/synthesizer step emits the same JSON-in-prose block.
- :func:`parse_findings` tolerantly extracts that block from an LLM reply —
  subagent steps return prose, so the contract is "a JSON array somewhere in the
  text", not "the text is JSON".
- :func:`render_findings_markdown` is the one human-facing rendering, used by the
  craft skill's report and the board gate's PR comment.

The workflow ENGINE stays string-based (steps thread text, ADR 0002); this module
is the contract layered on top, shared by producers (recipe prompts) and
consumers (craft `/code-review` skill, the projectBoard review gate, console).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

# Severity order — also the sort/grouping order for rendering.
SEVERITIES = ("blocker", "major", "minor", "nit")

# Verify-pass verdicts. The verifier's research vocabulary (SUPPORTED/UNSUPPORTED/
# UNCERTAIN) is accepted on parse and normalized to these.
VERDICTS = ("confirmed", "refuted", "uncertain")

_VERDICT_ALIASES = {
    "supported": "confirmed",
    "unsupported": "refuted",
    "false-positive": "refuted",
    "false positive": "refuted",
    "unverified": "uncertain",
    "plausible": "uncertain",
}

# The canonical prompt snippet. Role prompts and recipe steps interpolate this so
# the schema is written down exactly once.
FINDINGS_CONTRACT = """\
Report findings as a fenced JSON block, exactly this shape (and nothing else
inside the fence):

```json
[
  {
    "file": "path/to/file.py",
    "line": 42,
    "severity": "blocker | major | minor | nit",
    "category": "correctness | removed-behavior | cross-file | conventions | security | tests",
    "claim": "One-sentence statement of the defect.",
    "evidence": "The hunk/quote from the diff (or the concrete scenario) that shows it."
  }
]
```

Rules: `line` is the NEW-file line number (0 if not line-anchored). `claim` states
a defect, not a description of the code. `evidence` must quote or concretely
reference the diff — a finding you cannot evidence does not go in the list. No
findings → an empty array `[]`. Prose around the fence is fine (your reasoning);
the fenced array is the deliverable."""


@dataclass
class Finding:
    file: str = ""
    line: int = 0
    severity: str = "minor"
    category: str = ""
    claim: str = ""
    evidence: str = ""
    verdict: str = ""  # "" until a verify pass sets confirmed/refuted/uncertain
    note: str = field(default="")  # verifier's one-line justification, optional

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["verdict"]:
            d.pop("verdict")
        if not d["note"]:
            d.pop("note")
        return d


def _coerce(item: dict) -> Finding | None:
    """One raw dict → a Finding, or None if it isn't one (no claim)."""
    claim = str(item.get("claim") or item.get("summary") or "").strip()
    if not claim:
        return None
    try:
        line = int(item.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    severity = str(item.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        severity = "minor"
    verdict = str(item.get("verdict") or "").strip().lower()
    verdict = _VERDICT_ALIASES.get(verdict, verdict)
    if verdict not in VERDICTS:
        verdict = ""
    return Finding(
        file=str(item.get("file") or "").strip(),
        line=line,
        severity=severity,
        category=str(item.get("category") or "").strip().lower(),
        claim=claim,
        evidence=str(item.get("evidence") or "").strip(),
        verdict=verdict,
        note=str(item.get("note") or "").strip(),
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _candidate_arrays(text: str) -> list[list]:
    """Every parseable JSON array in ``text`` — fenced blocks first, then bare
    ``[...]`` spans (brace-matched, string-aware)."""
    out: list[list] = []
    for m in _FENCE_RE.finditer(text or ""):
        body = m.group(1).strip()
        if body.startswith("["):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                out.append(data)
    if out:
        return out
    # No fenced array — scan for bare top-level arrays.
    s = text or ""
    i = 0
    while (start := s.find("[", i)) != -1:
        depth, in_str, esc = 0, False, False
        for j in range(start, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(s[start : j + 1])
                        if isinstance(data, list):
                            out.append(data)
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            break
        if start >= i:  # unclosed array — stop scanning
            break
    return out


def parse_findings(text: str) -> list[Finding]:
    """Extract the findings array from an LLM reply, tolerantly.

    Picks the candidate JSON array with the most finding-shaped items (models
    sometimes echo an earlier step's list before their own — the fuller list is
    the deliverable; ties → the LAST one, the reply's conclusion). An explicit
    empty array parses as [] (a clean review), as does text with no array at all
    — callers that must distinguish should check for the fence themselves.
    """
    best: list[Finding] = []
    for arr in _candidate_arrays(text):
        findings = [f for item in arr if isinstance(item, dict) and (f := _coerce(item))]
        if len(findings) >= len(best):
            best = findings
    return best


def render_findings_markdown(findings: list[Finding], *, title: str = "Review findings") -> str:
    """The one human-facing rendering — grouped by severity, verdict-annotated."""
    if not findings:
        return f"## {title}\n\nNo findings — the review came back clean."
    by_sev: dict[str, list[Finding]] = {}
    for f in findings:
        by_sev.setdefault(f.severity if f.severity in SEVERITIES else "minor", []).append(f)
    lines = [f"## {title}", ""]
    counts = ", ".join(f"{len(by_sev[s])} {s}" for s in SEVERITIES if s in by_sev)
    lines.append(f"{len(findings)} finding(s): {counts}.")
    for sev in SEVERITIES:
        if sev not in by_sev:
            continue
        lines += ["", f"### {sev.capitalize()}", ""]
        for f in by_sev[sev]:
            loc = f"`{f.file}:{f.line}`" if f.file and f.line else (f"`{f.file}`" if f.file else "(no file)")
            verdict = f" — **{f.verdict}**" if f.verdict else ""
            cat = f" _[{f.category}]_" if f.category else ""
            lines.append(f"- {loc}{cat}{verdict}: {f.claim}")
            if f.evidence:
                lines.append(f"  - evidence: {f.evidence}")
            if f.note:
                lines.append(f"  - verifier: {f.note}")
    return "\n".join(lines)
