"""Redaction is LEAK-FREE first and SYNTAX-PRESERVING second.

Tool output is redacted before the model reads it (AuditMiddleware), so a pattern that
also eats the closing quote/bracket/comma after a token hands a code reviewer source
that no longer parses — how the review panel FAILed projectBoard-plugin#523: a test
file's `"Authorization: Bearer <jwt>",` reached it as `"Authorization: Bearer
[REDACTED]` and was reported as a SyntaxError that wasn't on disk. But the fix must
never trade that for a partial leak (`API_KEY=abc,def` → `API_KEY=[REDACTED],def`).

The contracts, table-driven over token shapes × syntactic contexts:

- **No leak (property):** the secret is gone, and so is every proper suffix of it 4+
  characters long — including secrets full of `, ; & ( [ { < ! #`.
- **Only the secret changes:** for ordinary tokens the output equals the input with
  the secret swapped for `[REDACTED]` — every delimiter survives in place. The one
  exception is deliberate: an UNQUOTED contextual value (`KEY=v`, `Bearer v`) runs
  to whitespace/quote/closing bracket, so a following `,next` / `;next` goes with it.
- **Still parses:** Python, JSON and YAML that parsed before redaction parse after.

Fixtures are assembled from pieces so no token-shaped literal sits in this file.
"""

from __future__ import annotations

import ast
import json

import pytest
import yaml

from graph.middleware.redaction import redact

_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxIn0" + "." + "s1G" * 4
_GHP = "ghp_" + "Ab1" * 12

# (id, carrier template — `{}` is where the secret sits, secret)
TOKENS = [
    # the projectBoard-plugin#523 shape, verbatim (header + non-JWT-payload token)
    ("bearer", "Authorization: Bearer {}", "eyJhbGciOiJIUzI1NiJ9" + ".payload.sig"),
    ("bearer_jwt", "Authorization: Bearer {}", _JWT),
    ("basic", "Authorization: Basic {}", "Ym9iOmh1bnRlcjI" + "="),
    ("jwt_bare", "{}", _JWT),
    ("ghp", "{}", _GHP),
    ("github_pat", "{}", "github_pat_" + "Ab3" * 8 + "_" + "Cd4" * 10),
    ("openai_sk", "{}", "sk-" + "Zy9" * 14),
    ("openai_sk_proj", "{}", "sk-proj-" + "Qx7" * 14),
    # base64url: `-` and `_` mid-token (pr #4166 review: `[A-Za-z0-9]` missed these)
    ("npm", "{}", "npm_" + "aB3-" * 5 + "c_D4" * 5),
    ("aws_access_key", "{}", "AKIA" + "ABCD2345" * 2),
    ("aws_sts_key", "{}", "ASIA" + "WXYZ6789" * 2),
    ("url_userinfo", "https://bob:{}@example.com/repo.git", "hunter2" + "pw" * 4),
    ("env_api_key", "API_" + "KEY={}", "v" + "9a" * 10),
    ("env_auth_token", "AUTH_" + "TOKEN={}", "t0k" + "eN" * 6),
    ("env_aws_secret", "AWS_SECRET_" + "ACCESS_KEY={}", "wJalr" + "Xu8" * 10),
    ("env_npm_token", "NPM_" + "TOKEN={}", "nT" + "k5" * 8),
    ("generic_api_key", "api_" + "key={}", "abcdefghijklmnop" + "1234"),
]
# Unquoted contextual values run to whitespace/quote/closing bracket — through `,` `;`.
_CONTEXTUAL = {
    "bearer",
    "bearer_jwt",
    "basic",
    "env_api_key",
    "env_auth_token",
    "env_aws_secret",
    "env_npm_token",
    "generic_api_key",
}

# Secrets carrying the characters a narrowed value class would stop at. Leak property
# only: in an unquoted separator context they legitimately consume what follows.
WEIRD_TOKENS = [
    ("env_punct", "API_" + "KEY={}", "ab,cd;ef&gh(ij[kl{mn<op!q#r"),
    ("env_aws_punct", "AWS_SESSION_" + "TOKEN={}", "Fw0G,zZ;9&x(y[w{v<u"),
    ("bearer_punct", "Authorization: Bearer {}", "abc!def#ghi,jkl;mno&pq"),
    ("basic_punct", "Authorization: Basic {}", "Ym9i(Oj[aH{VudGVy<Mg!"),
    ("generic_punct", "api_" + "key={}", "abcdefghijklmnop!x,y;z&w"),
    ("client_secret_punct", "client_" + "secret={}", "csABCDEFGHIJKL,mn;op"),
    ("url_password_with_at", "https://bob:{}@example.com/x", "p@ss,w;rd&9"),
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
_SEPARATOR_CONTEXTS = {"comma_separated", "semicolon"}


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


def _assert_no_leak(secret: str, out: str) -> None:
    assert secret not in out, out
    for i in range(1, len(secret) - 3):
        assert secret[i:] not in out, f"suffix {secret[i:]!r} leaked: {out!r}"


_ALL = TOKENS + WEIRD_TOKENS


@pytest.mark.parametrize("ctx_id,ctx", CONTEXTS, ids=[c[0] for c in CONTEXTS])
@pytest.mark.parametrize("tok_id,carrier,secret", _ALL, ids=[t[0] for t in _ALL])
def test_no_part_of_the_secret_survives_and_parseable_text_still_parses(tok_id, carrier, secret, ctx_id, ctx):
    original = ctx.format(carrier.format(secret))
    out = redact(original)
    _assert_no_leak(secret, out)
    if _parses(original, ctx_id):
        assert _parses(out, ctx_id), out


@pytest.mark.parametrize("ctx_id,ctx", CONTEXTS, ids=[c[0] for c in CONTEXTS])
@pytest.mark.parametrize("tok_id,carrier,secret", TOKENS, ids=[t[0] for t in TOKENS])
def test_only_the_secret_is_replaced(tok_id, carrier, secret, ctx_id, ctx):
    original = ctx.format(carrier.format(secret))
    out = redact(original)
    if tok_id in _CONTEXTUAL and ctx_id in _SEPARATOR_CONTEXTS:
        # Unquoted contextual value: `,`/`;` can sit inside a secret, so the value runs
        # on through them — over-redaction, never a leak.
        assert out == original.split(secret)[0] + "[REDACTED]"
    else:
        assert out == original.replace(secret, "[REDACTED]")


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


# ── quote-aware values ─────────────────────────────────────────────────────────

_K = "API_" + "KEY"


@pytest.mark.parametrize(
    "src,expected",
    [
        # unquoted: runs through `,` `;` `&` `(` `[` `{` `<` — no suffix left behind
        (_K + "=abc,def", _K + "=[REDACTED]"),
        (_K + "=abc;def&ghi", _K + "=[REDACTED]"),
        (_K + "=a(b[c{d<e f", _K + "=[REDACTED] f"),
        # unquoted, stops at a quote or closing bracket
        ('"' + _K + '=abc,def",', '"' + _K + '=[REDACTED]",'),
        ("(" + _K + "=abc)", "(" + _K + "=[REDACTED])"),
        # quoted value: everything up to the MATCHING quote, both quotes kept
        (_K + '="a,b;c d" rest', _K + '="[REDACTED]" rest'),
        (_K + "='x; y, z'", _K + "='[REDACTED]'"),
        (_K + "=`a b,c`", _K + "=`[REDACTED]`"),
        # the KEY=value text itself inside a string: to that string's closing quote
        ("check('" + _K + "=a b,c;d'),", "check('" + _K + "=[REDACTED]'),"),
        # JSON / dict spelling
        ('{"' + _K + '": "a, b; c", "n": 1}', '{"' + _K + '": "[REDACTED]", "n": 1}'),
        ("{'" + _K + "': 'a b'}", "{'" + _K + "': '[REDACTED]'}"),
        # an escaped quote inside the quoted value does not end it
        (_K + '="a\\"b,c" x', _K + '="[REDACTED]" x'),
    ],
)
def test_env_assignment_values_are_quote_aware(src, expected):
    assert redact(src) == expected


@pytest.mark.parametrize(
    "src,expected",
    [
        ("Authorization: Bearer abc!def", "Authorization: Bearer [REDACTED]"),
        ("Authorization: Bearer abc#def) x", "Authorization: Bearer [REDACTED]) x"),
        ('"Authorization: Bearer abc def!",', '"Authorization: Bearer [REDACTED]",'),
        ('{"Authorization": "Bearer a b,c"}', '{"Authorization": "Bearer [REDACTED]"}'),
        ("{'Authorization': 'token " + _GHP + "'}", "{'Authorization': 'token [REDACTED]'}"),
        (
            '{"headers": {"authorization": "bearer abc.def-ghi_123"}}',
            '{"headers": {"authorization": "bearer [REDACTED]"}}',
        ),
    ],
)
def test_bearer_values_are_quote_aware(src, expected):
    assert redact(src) == expected


@pytest.mark.parametrize(
    "benign",
    [
        "Authorization: Bearer ${TOKEN}",
        "Authorization: Bearer $TOKEN and more",
        'f"Authorization: Bearer {token}"',
        _K + "=${OPENAI}",
        "see https://example.com:8080/path@v2",
        "the token is in the header",
        "Bearer tokens are described in RFC 6750",
        "api_key = os.environ.get('X')",
        "API_" + 'KEY = os.environ["OPENAI"]',
        "AUTH_" + "TOKEN = getenv()",
    ],
)
def test_benign_text_is_untouched(benign):
    assert redact(benign) == benign


def test_query_string_value_is_fully_redacted():
    """`&` can sit inside a secret, so an unquoted value runs on through it."""
    out = redact("https://api.example.com/x?access_" + "token=abc123def456&page=2 next")
    assert out == "https://api.example.com/x?access_token=[REDACTED] next"


def test_url_userinfo_keeps_user_and_host():
    out = redact("git clone https://x-access-token:" + "ghs_" + "A" * 36 + "@github.com/o/r.git")
    assert out == "git clone https://x-access-token:[REDACTED]@github.com/o/r.git"


def test_url_userinfo_password_containing_at_is_redacted_to_the_last_at():
    out = redact("https://bob:" + "p@ss@word" + "@example.com/x")
    assert out == "https://bob:[REDACTED]@example.com/x"


@pytest.mark.parametrize("ref", ["${GH_TOKEN}", "$GH_TOKEN", "{token}", "{{ secrets.GH_TOKEN }}"])
def test_url_userinfo_template_ref_is_not_a_secret(ref):
    """#4546: frank#6 FAILed on a 'hardcoded token' that was `${GH_TOKEN}` behind the mask."""
    # Concatenated, like the test above: spelled inline, the unfixed redactor masks the
    # source line itself, and a reviewer reading through it sees no `{ref}` at all.
    line = 'git config --global url."https://x-access-token:' + ref + '@github.com/".insteadOf "https://github.com/"'
    assert redact(line) == line


def test_url_userinfo_password_merely_containing_a_ref_is_still_redacted():
    out = redact("https://bob:hunter2${SUFFIX}@example.com/x")
    assert out == "https://bob:[REDACTED]@example.com/x"


def test_redaction_is_idempotent():
    """A second pass (audit after transcript redaction) must not grow or re-wrap marks."""
    for _tok_id, carrier, secret in _ALL:
        for _ctx_id, ctx in CONTEXTS:
            once = redact(ctx.format(carrier.format(secret)))
            assert redact(once) == once, once


@pytest.mark.parametrize(
    "key", ["AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID", "NPM_TOKEN", "npm_token"]
)
def test_dict_form_secret_keys_are_redacted(key):
    out = redact({key: "some-value-123", "other": "kept"})
    assert out == {key: "[REDACTED]", "other": "kept"}


def test_aws_access_key_id_assignment_is_redacted():
    out = redact("AWS_ACCESS_" + "KEY_ID=" + "ASIA" + "WXYZ6789" * 2)
    assert out == "AWS_ACCESS_KEY_ID=[REDACTED]"


# ── the reviewer is told whose marks these are ─────────────────────────────────


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
