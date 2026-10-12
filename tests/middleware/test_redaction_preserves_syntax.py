"""Redaction is format-preserving: only a secret's own characters are replaced.

Tool output is redacted before the model reads it (AuditMiddleware), so a pattern that
also eats the closing quote/bracket/comma after a token hands a code reviewer source
that no longer parses. That is exactly how the review panel FAILed
projectBoard-plugin#523: a test file's `"Authorization: Bearer <jwt>",` reached the
reviewer as `"Authorization: Bearer [REDACTED]` and was reported as a SyntaxError
that wasn't on disk.

Table-driven: every token shape × every syntactic context. The contract, per case:
the secret is gone, and the redacted text equals the original with ONLY the secret
swapped for `[REDACTED]` — so every delimiter survives, and whatever parsed before
(Python, JSON, YAML) still parses after.

Fixtures are assembled from pieces so no token-shaped literal sits in this file.
"""

from __future__ import annotations

import ast
import json

import pytest
import yaml

from graph.middleware.redaction import redact

_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxIn0" + "." + "s" * 12

# (id, carrier template — `{}` is where the secret sits, secret)
TOKENS = [
    # the projectBoard-plugin#523 shape, verbatim (header + non-JWT-payload token)
    ("bearer", "Authorization: Bearer {}", "eyJhbGciOiJIUzI1NiJ9" + ".payload.sig"),
    ("bearer_jwt", "Authorization: Bearer {}", _JWT),
    ("basic", "Authorization: Basic {}", "Ym9iOmh1bnRlcjI" + "="),
    ("jwt_bare", "{}", _JWT),
    ("ghp", "{}", "ghp_" + "A" * 36),
    ("github_pat", "{}", "github_pat_" + "A" * 22 + "_" + "B" * 30),
    ("openai_sk", "{}", "sk-" + "Z" * 40),
    ("openai_sk_proj", "{}", "sk-proj-" + "Q" * 40),
    ("npm", "{}", "npm_" + "a" * 36),
    ("aws_access_key", "{}", "AKIA" + "A" * 16),
    ("url_userinfo", "https://bob:{}@example.com/repo.git", "hunter2" + "pw" * 4),
    ("env_api_key", "API_" + "KEY={}", "v" + "9" * 20),
    ("env_auth_token", "AUTH_" + "TOKEN={}", "t0k" + "e" * 12),
    ("env_aws_secret", "AWS_SECRET_" + "ACCESS_KEY={}", "wJalr" + "X" * 30),
    ("generic_api_key", "api_" + "key={}", "abcdefghijklmnop" + "1234"),
]

# (id, context template — `{}` is where the carrier text sits)
CONTEXTS = [
    ("py_dq_list_item", 'HEADERS = [\n    "{}",\n    "next",\n]\n'),
    ("py_sq_call_arg", "check('{}')\n"),
    ("py_dict_value", 'cfg = {{"k": "{}", "n": 1}}\n'),
    ("parenthesised", "call({})"),
    ("comma_separated", "{},next"),
    ("semicolon", "{};next"),
    ("square_brackets", "[{}]"),
    ("curly_braces", "{{{}}}"),
    ("angle_brackets", "<{}>"),
    ("backticks", "run `{}` now"),
    ("json_value", '{{"k": "{}", "n": 1}}'),
    ("yaml_quoted", 'k: "{}"\nnext: 1\n'),
    ("yaml_single_quoted", "k: '{}'\nnext: 1\n"),
    ("end_of_line", "{}\nnext line"),
    ("end_of_text", "{}"),
]


def _parses(text: str, ctx: str) -> bool | None:
    """Whether ``text`` parses in the context's language; None when not a language."""
    try:
        if ctx.startswith("py_"):
            ast.parse(text)
        elif ctx.startswith("json"):
            json.loads(text)
        elif ctx.startswith("yaml"):
            yaml.safe_load(text)
        else:
            return None
    except (SyntaxError, ValueError, yaml.YAMLError):
        return False
    return True


@pytest.mark.parametrize("ctx_id,ctx", CONTEXTS, ids=[c[0] for c in CONTEXTS])
@pytest.mark.parametrize("tok_id,carrier,secret", TOKENS, ids=[t[0] for t in TOKENS])
def test_only_the_secret_is_replaced(tok_id, carrier, secret, ctx_id, ctx):
    original = ctx.format(carrier.format(secret))
    out = redact(original)
    assert secret not in out
    # Exactly the secret's characters swapped — every quote, bracket, comma,
    # semicolon and newline around it is still there, in place.
    assert out == original.replace(secret, "[REDACTED]")
    if _parses(original, ctx_id):
        assert _parses(out, ctx_id), out


def test_the_projectboard_523_line_still_parses():
    """The exact source that drew a false SyntaxError finding."""
    src = (
        "LEAKY = [\n"
        '    "Authorization: Bearer ' + "eyJhbGciOiJIUzI1NiJ9" + '.payload.sig",\n'
        '    "OPENAI key ' + "sk-" + "Z" * 40 + '",\n'
        "]\n"
    )
    out = redact(src)
    assert out == ('LEAKY = [\n    "Authorization: Bearer [REDACTED]",\n    "OPENAI key [REDACTED]",\n]\n')
    ast.parse(out)


_GHP = "ghp_" + "A" * 36


@pytest.mark.parametrize(
    "src,secret,load",
    [
        ('{"Authorization": "Bearer ' + _JWT + '", "n": 1}', _JWT, json.loads),
        ("{'Authorization': 'token " + _GHP + "'}", _GHP, ast.literal_eval),
        ('{"headers": {"authorization": "bearer abc.def-ghi_123"}}', "abc.def-ghi_123", json.loads),
    ],
)
def test_dict_spelled_authorization_header_is_redacted_and_parses(src, secret, load):
    out = redact(src)
    assert out == src.replace(secret, "[REDACTED]")
    load(out)


def test_query_string_value_stops_at_the_next_parameter():
    out = redact("https://api.example.com/x?access_" + "token=abc123def456&page=2")
    assert out == "https://api.example.com/x?access_token=[REDACTED]&page=2"


def test_url_userinfo_keeps_user_and_host():
    out = redact("git clone https://x-access-token:" + "ghs_" + "A" * 36 + "@github.com/o/r.git")
    assert out == "git clone https://x-access-token:[REDACTED]@github.com/o/r.git"


def test_redaction_is_idempotent():
    """A second pass (audit after transcript redaction) must not grow or re-wrap marks."""
    for _tok_id, carrier, secret in TOKENS:
        once = redact('x = ["' + carrier.format(secret) + '", 1]')
        assert redact(once) == once, once


@pytest.mark.parametrize(
    "benign",
    [
        "Authorization: Bearer ${TOKEN}",
        "see https://example.com:8080/path@v2",
        "the token is in the header",
        "Bearer tokens are described in RFC 6750",
    ],
)
def test_benign_text_is_untouched(benign):
    assert redact(benign) == benign


@pytest.mark.parametrize("role", ["review-finder", "review-synthesizer", "verifier"])
def test_every_code_review_role_is_told_redaction_marks_are_not_code(role):
    """Belt to the format-preserving braces: a residual `[REDACTED]` artefact must never
    become a finding, so every role that reads redacted tool output is told whose mark
    it is."""
    from graph.review.findings import REDACTION_NOTE
    from graph.subagents.config import SUBAGENT_REGISTRY

    prompt = SUBAGENT_REGISTRY[role].system_prompt
    assert REDACTION_NOTE in prompt
    assert "NEVER report a `[REDACTED]` mark" in prompt
