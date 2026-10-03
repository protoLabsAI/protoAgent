#!/usr/bin/env python3
"""Assert a QA-panel verdict exists for the head a PR would actually merge (ADR 0078 D3/D5).

The QA panel (`protoreview[bot]`) is **not** one of the required checks on `main`, so
nothing gates a merge on a review — and its absence is *silent*: no status is posted at
all, so nothing turns red, `mergeStateStatus` stays `CLEAN`, and the PR looks finished.
An unreviewed merge is therefore indistinguishable from a reviewed one at a glance.

Observed on `main` before this gate existed:

* **#3298** merged at head ``7721e5b9`` while the panel had only ever reviewed
  ``373d2759`` — the code that landed was never reviewed.
* **#3301** put 65 files / ~6,900 lines on `main` through an integration branch with no
  panel verdict at all.
* 9 of the 25 most recent PRs had no panel review whatsoever, and PR size does not
  predict which — so this is not ADR 0078 D2's structural trigger being selective.

ADR 0078 already forbids exactly this: **D3** ("a promotable verdict from a starved run is
how an unreviewed PR auto-merges") and **D5** (an advanced head gets a delta review). This
script makes the requirement observable instead of assumed, by posting a commit status —
always, on every open PR — that answers one question:

    Is there a QA-panel verdict for THIS head SHA?

It deliberately does **not** re-judge code. Verdict *quality* is the panel's own business
and it posts its own ``QA panel`` status for that; a ``WARN`` at head satisfies this gate
(#3297 shipped one). What fails here: no verdict for the head, an explicitly blocking
verdict, and — once the producer contract is live (#3334) — a verdict whose review
**coverage is not complete** or that **retains a standing block**. That keeps the gate safe
to mark **required** without making the advisory tier of ADR 0078 secretly mandatory.

**The coverage / standing-block contract (#3334).** A promotable verdict is not enough: a
PASS emitted while a structural lane was skipped (gateway exit 4) or while the panel brief
was unreadable has *not actually reviewed the head*, and a PASS can be posted while a
standing finding remains unresolved. So the panel stamps two further **producer-owned**
facts as their own marker attributes — ``coverage`` (``complete`` vs. incomplete/unavailable)
and ``standing_block`` (``false`` vs. an unresolved block) — and this gate consumes them
**fail-closed**: a head merges only when coverage is explicitly complete *and* no standing
block remains. These are independent attributes; we never infer either from the review prose
(that would re-judge the code, which this script does not do) and we do not fold them into a
widened verdict enum. Coverage and the standing block get their own status reason so a red
check says *which* one blocked. Missing / unknown attributes are non-satisfying, but only
after ``REQUIRE_COVERAGE_CONTRACT`` is turned on — the emitter and approve-on-green promoter
ship the attributes FIRST (they are outside this repo), and enforcing fail-closed against
today's legacy markers, which carry neither attribute, would red every open PR. An
*explicit* attribute is always honoured regardless of the flag; the flag only governs how an
*absent* attribute is treated.

Escape hatch: the ``skip-review-gate`` label passes the check with the reason recorded in
the status description — the same shape as ``skip-changelog`` and ``gate-exempt``. It
exists because a required check that can never go green (panel outage, a PR the panel does
not pick up) would otherwise wedge the queue with no way out but an admin merge.

**Which verdict speaks for a head (pr-reviewer-plugin#234).** Several panel rounds can land
on one head: two racing panels (pr-reviewer-plugin#89), or a re-review an operator summoned
to dispute a verdict. This gate reads them exactly as the plugin's own ``QA panel`` gate
does — the STRICTEST round wins (FAIL > WARN > PASS; promotions are not rounds), except that
a FAIL a later round *supersedes* drops out first: the plugin's pure rule
(``rounds.superseded_fails``), VENDORED below so this file stays self-contained in repos with
no plugin checkout. It used to read the LATEST marker, so after a FAIL a re-review PASS on
the same head turned this check green while ``QA panel`` stayed red. If the rule raises,
nothing is superseded and the strictest round wins (fail-closed).

Stdlib + ``gh`` only, so CI needs no dependency install. Pure decision logic lives in
``decide()`` and is covered by ``tests/test_review_at_head.py``; everything above it is I/O.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# The panel stamps every review body with a machine-readable marker, e.g.
#   <!-- protoagent-qa-review head=4fec0e53… verdict=PASS promoted=true findings=1 -->
# Parsed rather than inferred from the review's GitHub state, because a passing review is
# posted as COMMENTED and only *promoted* to APPROVED once checks are green and threads are
# resolved (ADR 0078 D2) — so an APPROVED state is a stricter thing than "was reviewed",
# and #3311 merged with a PASS that was never promoted.
_MARKER = re.compile(r"<!--\s*protoagent-qa-review\s+(?P<attrs>.*?)-->", re.DOTALL)
_ATTR = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")

REVIEWER_LOGIN = os.environ.get("REVIEWER_LOGIN", "protoreview[bot]")
STATUS_CONTEXT = os.environ.get("STATUS_CONTEXT", "Review at head")
SKIP_LABEL = "skip-review-gate"

# Verdicts that are an explicit "do not merge". PASS and WARN both satisfy the gate; WARN is
# advisory by design (ADR 0078) and #3297 shipped one. Kept as a set so a new blocking
# verdict name only has to be added here.
BLOCKING_VERDICTS = frozenset({"FAIL", "BLOCK", "REJECT"})

# The two producer-owned facts of the #3334 contract, carried as their own marker attributes
# alongside the verdict — NOT inferred from prose and NOT folded into the verdict enum.
COVERAGE_ATTR = "coverage"
STANDING_ATTR = "standing_block"
# The only attribute values that satisfy the gate (compared case-insensitively). Anything
# else — incomplete/unavailable coverage, a retained block, or an unknown/malformed value —
# is non-satisfying and fails closed.
COVERAGE_COMPLETE = "complete"
STANDING_CLEAR = "false"

# Rollout gate for the contract above. The QA-panel emitter and approve-on-green promoter are
# outside this repo and must ship authoritative `coverage`/`standing_block` attributes FIRST;
# until then every legacy marker carries neither, so enforcing fail-closed on their ABSENCE
# would red every open PR. Hence this stays OFF by default and is flipped (a repo variable
# wired in the workflow) once the producer rollout is live. An attribute that IS present is
# always honoured regardless of this flag — it only decides how a MISSING attribute is read.
REQUIRE_COVERAGE_CONTRACT = os.environ.get("REQUIRE_COVERAGE_CONTRACT", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@dataclass(frozen=True)
class Decision:
    """A commit-status outcome: ``state`` is GitHub's, ``description`` is what a human reads."""

    state: str  # "success" | "failure"
    description: str

    @property
    def ok(self) -> bool:
        return self.state == "success"


def parse_marker(body: str | None) -> dict[str, str] | None:
    """The marker attributes in one review body, or None when it carries no marker.

    Tolerant on purpose: an unparseable or marker-less body is "not a verdict" rather than an
    error, so a human comment from the reviewer account can never be mistaken for a review.
    """
    if not body:
        return None
    found = _MARKER.search(body)
    if not found:
        return None
    return {m["key"]: m["value"] for m in _ATTR.finditer(found["attrs"])}


# ── BEGIN VENDORED SUPERSEDE RULE ─────────────────────────────────────────────────────
# Vendored from protoLabsAI/pr-reviewer-plugin `rounds.py` (+ the findings-record reader in
# `verdicts.py`) @ f0f0bf5 — the option-(a) rule of pr-reviewer-plugin#234. Copied, not
# imported: the repos this script gates have no plugin checkout, and a network fetch would
# make a required check depend on it. Do NOT edit here; edit the plugin and re-sync.
#   rounds-ast-sha256: 04e26b5f1fe0f7a29a0f88d11c400c97f26111435dd55ed1ea70790fba8262c4
#   block-ast-sha256:  0e0cb7cc11df1b3a1605209a0f8daf27cbc524817223a9f758eb0a0deca730e9
# Both hashes are over the AST minus comments and docstrings (formatter-proof; see
# pr-reviewer-plugin `scripts/vendor_supersede_rule.py`). The plugin's drift
# test fails when `rounds.py`'s rule no longer matches the first, or this block the second;
# each downstream copy's own test fails when its block no longer matches the second.

_V_BLOCKING = ("blocker", "major")
_V_RECORD_SUMMARY = "findings JSON (machine-readable)"
_V_RECORD_RE = re.compile(
    r"<details>[ \t]*\r?\n<summary>" + re.escape(_V_RECORD_SUMMARY) + r"</summary>[ \t]*(?:\r?\n[ \t]*)+"
    r"```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?\n</details>",
    re.DOTALL,
)
_V_MAX_RECORD_CHARS = 16_000
_V_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _v_norm(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def _v_anchor(file: object, line: object) -> str:
    path = _v_norm(str(file or ""))
    return f"{path}:{line}" if isinstance(line, int) else path


def _v_review_id(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _v_findings_record(body: str) -> tuple[list[dict], bool]:
    """(findings, recorded): `recorded` only for exactly ONE well-formed findings record.
    The rule never reads the findings of an unrecorded round, so none are recalled."""
    blocks = _V_RECORD_RE.findall(body or "")
    if len(blocks) != 1:
        return [], False
    try:
        parsed = json.loads(blocks[0]) if blocks[0].strip() else None
    except json.JSONDecodeError:
        return [], False
    if not isinstance(parsed, list):
        return [], False
    findings = [f for f in parsed if isinstance(f, dict)]
    return findings, len(findings) == len(parsed)


def _v_decode_disposition_record(token: object) -> dict | None:
    if not isinstance(token, str) or not token or len(token) > _V_MAX_RECORD_CHARS:
        return None
    if not _V_B64URL_RE.match(token):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        return None
    of = parsed.get("of")
    if not isinstance(of, int) or isinstance(of, bool) or of <= 0:
        return None
    rows = []
    for row in parsed["rows"]:
        if not (
            isinstance(row, dict)
            and isinstance(row.get("a"), str)
            and isinstance(row.get("d"), str)
            and isinstance(row.get("e"), bool)
            and isinstance(row.get("h"), bool)
        ):
            return None
        rows.append({"a": row["a"], "d": row["d"], "e": row["e"], "h": row["h"]})
    return {"of": of, "rows": rows}


def _v_panel_round(attrs: dict[str, str], body: str, review_id) -> dict:
    """One posted, non-promotion review → the round facts the rule reads."""
    findings, recorded = _v_findings_record(body)
    return {
        "head": attrs.get("head", ""),
        "verdict": attrs.get("verdict", ""),
        "findings": findings,
        "findings_recorded": recorded,
        "complete": attrs.get("complete", "true").lower() != "false",
        "verified": attrs.get("verified", "true").lower() != "false",
        "reaffirmed": attrs.get("reaffirmed", ""),
        "id": _v_review_id(review_id),
        "disposed": _v_decode_disposition_record(attrs.get("disp")),
    }


def _v_blocking_priors(round_: dict | None) -> list[dict]:
    return [
        f
        for f in (round_ or {}).get("findings") or []
        if isinstance(f, dict)
        and str(f.get("severity") or "").lower() in _V_BLOCKING
        and str(f.get("verdict") or "").lower() != "refuted"
        and not f.get("nearby")
        and not f.get("ungrounded")
    ]


def _v_supersedes(fail: dict, newer: dict) -> bool:
    """Does `newer` replace `fail` as the verdict for their head? Fails CLOSED.

    Only a COMPLETE, VERIFIED round on the same head, posted after `fail`, that
    dispositioned `fail` itself and refuted EVERY blocking finding of it with evidence,
    the refutation honoured, and that does not still hold one of them."""
    if not isinstance(fail, dict) or not isinstance(newer, dict):
        return False
    if str(fail.get("verdict") or "").upper() != "FAIL" or fail.get("reaffirmed") or not fail.get("findings_recorded"):
        return False
    fail_id = _v_review_id(fail.get("id"))
    newer_id = _v_review_id(newer.get("id"))
    if fail_id <= 0 or newer_id <= fail_id:
        return False
    if not fail.get("head") or newer.get("head") != fail.get("head"):
        return False
    if newer.get("reaffirmed"):
        return False
    if not (newer.get("complete") is True and newer.get("verified") is True and newer.get("findings_recorded") is True):
        return False
    record = newer.get("disposed")
    if not isinstance(record, dict) or record.get("of") != fail_id:
        return False
    priors = _v_blocking_priors(fail)
    if not priors:
        return False
    rows: dict[str, list[dict]] = {}
    for row in record.get("rows") or []:
        rows.setdefault(str(row.get("a") or ""), []).append(row)
    still = {_v_anchor(f.get("file"), f.get("line")) for f in _v_blocking_priors(newer)}
    for prior in priors:
        anchor = _v_anchor(prior.get("file"), prior.get("line"))
        mine = rows.get(anchor) or []
        if not mine or anchor in still:
            return False
        if not all(r.get("d") == "refuted" and r.get("e") is True and r.get("h") is True for r in mine):
            return False
    return True


def _v_superseded_fails(rounds: list[dict]) -> list[tuple[dict, dict]]:
    pool = [r for r in rounds or [] if isinstance(r, dict)]
    out = []
    for fail in pool:
        by = next((r for r in pool if r is not fail and _v_supersedes(fail, r)), None)
        if by is not None:
            out.append((fail, by))
    return out


# ── END VENDORED SUPERSEDE RULE ───────────────────────────────────────────────────────


# Strictest first. A marker with no verdict outranks everything, so it is picked and fails
# ("carries no verdict"); an unknown verdict ranks with WARN.
def _rank(attrs: dict[str, str]) -> int:
    if "verdict" not in attrs:
        return 3
    verdict = attrs["verdict"].upper()
    if verdict in BLOCKING_VERDICTS:
        return 2
    return {"PASS": 0, "WARN": 1}.get(verdict, 1)


def _superseded(panel: list[tuple[dict, dict[str, str]]]) -> set[int]:
    """Indices into `panel` of FAIL rounds a later round supersedes (the vendored rule).
    A rule that raises supersedes nothing: the strictest round wins (fail-closed)."""
    try:
        rounds = [_v_panel_round(attrs, review.get("body") or "", review.get("id")) for review, attrs in panel]
        gone = {id(fail) for fail, _by in _v_superseded_fails(rounds)}
        return {index for index, round_ in enumerate(rounds) if id(round_) in gone}
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ strictest-wins
        print(f"review_at_head: supersede rule failed ({exc!r}); strictest verdict wins", file=sys.stderr)
        return set()


def verdict_for_head(reviews: list[dict], head_sha: str) -> dict[str, str] | None:
    """The marker that speaks for ``head_sha``, or None when there is none.

    The STRICTEST panel round for the head, after dropping any FAIL a later round supersedes
    (pr-reviewer-plugin#234) — the same answer the plugin's ``QA panel`` gate reaches.
    Promotions (``promoted=true``) are not rounds; they speak only when the head has no
    round at all, as the newest of them. Ties go to the newest. Matching is on the full SHA —
    a prefix match would let a review of a *different* commit satisfy the gate on a
    collision, which is the whole failure this guards.
    """
    markers: list[tuple[dict, dict[str, str]]] = []
    for review in reviews:
        if (review.get("user") or {}).get("login") != REVIEWER_LOGIN:
            continue
        attrs = parse_marker(review.get("body"))
        if attrs and attrs.get("head") == head_sha:
            markers.append((review, attrs))
    if not markers:
        return None
    panel = [(r, a) for r, a in markers if a.get("promoted", "false").lower() != "true"]
    if not panel:
        return markers[-1][1]
    gone = _superseded(panel)
    pool = [a for i, (_r, a) in enumerate(panel) if i not in gone]
    worst = max(_rank(a) for a in pool)
    pick = next(a for a in reversed(pool) if _rank(a) == worst)
    return {**pick, "_superseded": str(len(gone))} if gone else pick


def _contract_failure(attrs: dict[str, str], head_sha: str) -> str | None:
    """The reason the coverage/standing-block contract (#3334) is not satisfied, else None.

    Coverage completeness and standing-block clearance are INDEPENDENT producer-owned facts:
    each is checked on its own attribute, and each yields its own status reason so a red check
    says which one blocked. A missing or unknown value is non-satisfying (fail closed) — the
    caller only reaches here once the attribute is present or the rollout flag requires it.
    The standing block is checked first because it is a do-not-merge in its own right, holding
    even when coverage is complete and the verdict is PASS.
    """
    head = head_sha[:12]

    standing = attrs.get(STANDING_ATTR)
    if standing is None:
        return f"panel marker for {head} carries no {STANDING_ATTR} — cannot confirm the block is cleared"
    if standing.strip().lower() != STANDING_CLEAR:
        return f"QA panel retains a standing block ({STANDING_ATTR}={standing}) at {head}"

    coverage = attrs.get(COVERAGE_ATTR)
    if coverage is None:
        return f"panel marker for {head} carries no {COVERAGE_ATTR} — review coverage is unconfirmed"
    if coverage.strip().lower() != COVERAGE_COMPLETE:
        return f"QA panel review coverage is {coverage} (not complete) at {head}"

    return None


def decide(
    reviews: list[dict],
    head_sha: str,
    labels: list[str],
    *,
    require_contract: bool = False,
) -> Decision:
    """Whether this head may merge, given the reviews on it. Pure — the tested seam.

    ``require_contract`` is the #3334 rollout gate: when true, a marker MISSING the
    coverage/standing-block attributes fails closed. An attribute that is present is always
    enforced regardless of the flag.
    """
    if SKIP_LABEL in labels:
        return Decision("success", f"review gate waived by the {SKIP_LABEL} label")

    attrs = verdict_for_head(reviews, head_sha)
    if attrs is None:
        reviewed = sorted(
            {
                a["head"][:12]
                for r in reviews
                if (a := parse_marker(r.get("body"))) and "head" in a
            }
        )
        if reviewed:
            # The dangerous case, and the reason this gate exists: the panel DID review, so
            # the PR carries a green verdict and reads as reviewed — but not this code.
            return Decision(
                "failure",
                f"no verdict for {head_sha[:12]}; the panel reviewed {', '.join(reviewed)} — push re-review or re-run",
            )
        return Decision("failure", f"no QA panel verdict for {head_sha[:12]} — this head is unreviewed")

    # Fail closed on a marker that matches the head but carries no verdict: "?" is not in
    # BLOCKING_VERDICTS, so it used to fall through to success. This gate exists to answer
    # "is there a verdict for THIS head" — a marker without one is not a verdict, and the
    # module treats every other missing critical attribute as non-satisfying.
    if "verdict" not in attrs:
        return Decision("failure", f"panel marker for {head_sha[:12]} carries no verdict")
    verdict = attrs.get("verdict", "?").upper()
    if verdict in BLOCKING_VERDICTS:
        return Decision("failure", f"QA panel returned {verdict} for {head_sha[:12]}")

    # Coverage completeness and standing-block clearance (#3334). Enforce when the marker
    # actually carries either attribute (an explicit producer fact is always honoured) or when
    # the rollout flag requires the contract (absent attributes then fail closed). Note: CI
    # status is not an input to this function, so a green build can never rescue a failure
    # here, and the check reads only marker attributes, never the review prose.
    contract_present = COVERAGE_ATTR in attrs or STANDING_ATTR in attrs
    if require_contract or contract_present:
        reason = _contract_failure(attrs, head_sha)
        if reason is not None:
            return Decision("failure", reason)

    if attrs.get("_superseded"):
        return Decision("success", f"{verdict} at {head_sha[:12]} (an earlier FAIL was refuted with evidence)")
    return Decision("success", f"{verdict} at {head_sha[:12]}")


# ── I/O ───────────────────────────────────────────────────────────────────────


def _gh(*args: str) -> str:
    """`gh` with the ambient token. Raises on failure — a broken API call must not be
    mistaken for "no verdict" and silently fail a PR that was in fact reviewed."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _reviews(pr: int) -> list[dict]:
    return json.loads(_gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{pr}/reviews"))


def _open_prs() -> list[dict]:
    return json.loads(_gh("pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefOid,labels"))


def _post_status(sha: str, decision: Decision, pr: int) -> None:
    _gh(
        "api",
        "-X",
        "POST",
        f"repos/{{owner}}/{{repo}}/statuses/{sha}",
        "-f",
        f"state={decision.state}",
        "-f",
        f"context={STATUS_CONTEXT}",
        # GitHub truncates descriptions past 140 chars.
        "-f",
        f"description={decision.description[:140]}",
        "-f",
        f"target_url={os.environ.get('RUN_URL', '')}",
    )
    print(f"#{pr} {sha[:12]} -> {decision.state}: {decision.description}")


def check_pr(pr: int, head_sha: str, labels: list[str], *, dry_run: bool) -> Decision:
    decision = decide(_reviews(pr), head_sha, labels, require_contract=REQUIRE_COVERAGE_CONTRACT)
    if dry_run:
        print(f"[dry-run] #{pr} {head_sha[:12]} -> {decision.state}: {decision.description}")
    else:
        _post_status(head_sha, decision, pr)
    return decision


def main() -> int:
    # Parsed like REQUIRE_COVERAGE_CONTRACT above, not with bool(): every non-empty string is
    # truthy, so `DRY_RUN=0` / `false` / `no` used to ENABLE dry-run — the gate would post no
    # status at all and look like it was working. A silent off-switch on a merge gate.
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
    pr_number = os.environ.get("PR_NUMBER")

    if pr_number:
        head = os.environ["HEAD_SHA"]
        labels = [x for x in os.environ.get("PR_LABELS", "").split(",") if x]
        # Always exit 0: the STATUS is the signal, not this job. A non-zero exit would add a
        # second red check saying the same thing, and would make an API hiccup look like an
        # unreviewed PR. That promise needs the same guard the sweep below has had from the
        # start: without it a RuntimeError from `_gh` (a dropped API call, a rate limit)
        # escapes main(), `raise SystemExit(main())` never runs, and the job exits non-zero
        # on a transient error — producing exactly the second red check this comment forbids.
        try:
            check_pr(int(pr_number), head, labels, dry_run=dry_run)
        except RuntimeError as exc:  # a transient API failure is not an unreviewed PR
            print(f"#{pr_number}: {exc}", file=sys.stderr)
        return 0

    # Sweep mode (scheduled backstop) — webhooks do get dropped here, and a PR whose
    # `pull_request_review` event was missed would otherwise sit with a stale red status.
    for row in _open_prs():
        labels = [lbl["name"] for lbl in row.get("labels") or []]
        try:
            check_pr(row["number"], row["headRefOid"], labels, dry_run=dry_run)
        except RuntimeError as exc:  # one unreachable PR must not abandon the rest
            print(f"#{row['number']}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
