#!/usr/bin/env python3
"""Redact PII/secrets from fleet trace dumps before they enter the training corpus.

The exporter (observability/trace_export.py) writes RAW dumps on-box; this batch
redactor runs at the SINK (invoked by sync_fleet_traces.sh) and only its REDACTED
output reaches the shared dataset dir. Hybrid, per the #1897 contract:

  1. Deterministic regex — API keys / tokens / JWTs / private keys / emails /
     phones. Exact, zero-infra, catches novel secret formats a model misses.
  2. openai/privacy-filter — free-form PII (names, addresses, account numbers,
     private dates/urls) regex can't catch. 1.5B sparse-MoE (50M active params),
     Apache-2.0, ~96% F1 on PII-Masking-300k, runs on CPU.

Irreversible masking to placeholders — the lab wants interaction *shape*, not
recoverable values, so there is deliberately NO reversible mapping. Applied to
message content, tool-call arguments, and meta.orient; never to structural
fields (roles, tool names, ids, tool schemas), which carry no PII.

FAIL-CLOSED: if the model can't load, this errors out rather than shipping
regex-only (under-redacted) data to the shared corpus — unless you explicitly
opt into regex-only with FLEET_REDACT_MODEL=none.

Usage:  redact_fleet_traces.py <in.jsonl> <out.jsonl>
Env:    FLEET_REDACT_MODEL   model id (default openai/privacy-filter; "none" =
                             regex-only, explicit opt-out of the ML layer)
        FLEET_REDACT_MIN_SCORE  span score threshold (default 0.5)
"""

from __future__ import annotations

import json
import os
import re
import sys

# ── Layer 1: deterministic secret/token patterns (high precision) ──────────────
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[PRIVATE_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[JWT]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), "[TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "[TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"), "Bearer [TOKEN]"),
    # Deterministic backstops for the two PII types the model also covers.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"), "[PHONE]"),
]

# ── Layer 2: openai/privacy-filter entity → placeholder ───────────────────────
_ENTITY_PLACEHOLDER = {
    "account_number": "[ACCOUNT]",
    "private_address": "[ADDRESS]",
    "private_email": "[EMAIL]",
    "private_person": "[NAME]",
    "private_phone": "[PHONE]",
    "private_url": "[URL]",
    "private_date": "[DATE]",
    "secret": "[SECRET]",
}

_MODEL_NAME = os.environ.get("FLEET_REDACT_MODEL", "openai/privacy-filter").strip()
_MIN_SCORE = float(os.environ.get("FLEET_REDACT_MIN_SCORE", "0.5"))
_pipe = None
_cache: dict[str, str] = {}


def _load_model():
    """Load the token-classification pipeline. Returns None only for the explicit
    regex-only opt-out; any OTHER failure raises (fail-closed)."""
    if _MODEL_NAME.lower() in ("none", "off", ""):
        print("[redact] FLEET_REDACT_MODEL=none — regex-only (ML layer disabled)", file=sys.stderr)
        return None
    from transformers import pipeline  # heavy import — deferred to actual use

    return pipeline(
        "token-classification",
        model=_MODEL_NAME,
        aggregation_strategy="simple",
    )


def _model_redact(text: str) -> str:
    """Mask model-detected PII spans, replacing right-to-left to keep offsets valid."""
    if _pipe is None or not text.strip():
        return text
    spans = [s for s in _pipe(text) if s.get("score", 1.0) >= _MIN_SCORE]
    for s in sorted(spans, key=lambda x: x["start"], reverse=True):
        placeholder = _ENTITY_PLACEHOLDER.get(s.get("entity_group", ""), "[PII]")
        # Model spans often include leading whitespace — preserve it so
        # "You are Jon" masks to "You are [NAME]", not "You are[NAME]".
        start = s["start"]
        while start < s["end"] and text[start].isspace():
            start += 1
        text = text[:start] + placeholder + text[s["end"] :]
    return text


def redact(text: str) -> str:
    """Full hybrid pass: deterministic secrets/PII regex, then the model for
    free-form PII. Memoized — the (identical) system prompt recurs every row."""
    if not text:
        return text
    hit = _cache.get(text)
    if hit is not None:
        return hit
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    out = _model_redact(out)
    _cache[text] = out
    return out


def redact_row(row: dict) -> dict:
    """Redact the free-text surfaces of one Trajectory row in place."""
    for m in row.get("messages", []) or []:
        if isinstance(m.get("content"), str):
            m["content"] = redact(m["content"])
        for tc in m.get("tool_calls", []) or []:
            if isinstance(tc.get("arguments"), str):
                tc["arguments"] = redact(tc["arguments"])
    meta = row.setdefault("meta", {})
    if isinstance(meta.get("orient"), str) and meta["orient"]:
        meta["orient"] = redact(meta["orient"])
    meta["redacted"] = True
    meta["redaction"] = {"regex": True, "model": (_MODEL_NAME if _pipe is not None else None)}
    return row


def main(argv: list[str]) -> int:
    global _pipe
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, dst = argv[1], argv[2]
    _pipe = _load_model()

    n_in = n_out = n_err = 0
    tmp = dst + ".tmp"
    with open(src, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            try:
                row = redact_row(json.loads(line))
                fout.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                n_out += 1
            except Exception as e:  # noqa: BLE001 — skip a bad line, never emit it raw
                n_err += 1
                print(f"[redact] skipped malformed row {n_in}: {e}", file=sys.stderr)
    os.replace(tmp, dst)
    print(f"[redact] {src} -> {dst}: {n_out} redacted, {n_err} skipped (model={_MODEL_NAME if _pipe else 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
