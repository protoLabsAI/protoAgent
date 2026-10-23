"""Hand-built, redacted spike shapes: no live accounts or credentials."""

import copy
import json
from unittest.mock import Mock

import httpx
import pytest

from graph.providers import oauth, usage
from graph.providers.usage import classify, usable_for
from graph.providers.usage.anthropic import fetch_anthropic_usage, parse_anthropic_usage
from graph.providers.usage.codex import fetch_codex_usage, parse_codex_usage
from infra.paths import InstancePaths

RESET = "2026-10-23T12:00:00Z"
CLAUDE = {"plan": "max", "ignored": True, "limits": [
    {"kind": "5h", "percent": 25, "severity": "ok", "resets_at": RESET},
    {"kind": "7d", "percent": 81, "resets_at": RESET},
    {"kind": "weekly", "percent": 50, "severity": "blocked", "resets_at": RESET,
     "scope": {"model": "Fable"}},
]}
PRIMARY = {"used_percent": 20, "limit_window_seconds": 18000, "reset_at": 1792756800}
SECONDARY = {"used_percent": 85, "limit_window_seconds": 604800, "reset_at": 1792756800}
CODEX = {"plan_type": "pro", "rate_limit": {"primary_window": PRIMARY, "secondary_window": SECONDARY},
         "additional_rate_limits": [{"model": "test-model", "limit_name": "test-bucket",
                                     "rate_limit": {"primary_window": {**PRIMARY, "used_percent": 100}}}],
         "credits": {"balance": "10", "has_credits": True}}
PROVIDERS = [
    (fetch_anthropic_usage, parse_anthropic_usage, "peek_anthropic_access_token", CLAUDE),
    (fetch_codex_usage, parse_codex_usage, "peek_codex_access_token", CODEX),
]


@pytest.fixture(autouse=True)
def no_refresh_or_write(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("usage must not refresh, resolve, lock or write credentials")
    for name in ("_refresh_anthropic_tokens", "_refresh_codex_tokens", "resolve_anthropic_oauth",
                 "resolve_codex_oauth", "_write_anthropic_store", "_write_codex_store", "_oauth_scope_lock"):
        monkeypatch.setattr(oauth, name, forbidden)
    monkeypatch.setattr(usage, "_warned", set())


def test_claude_golden():
    account = parse_anthropic_usage(CLAUDE)
    assert account.status == "ok"
    assert [w.kind for w in account.windows] == ["session", "weekly", "weekly"]
    assert [w.status for w in account.windows] == ["ok", "warning", "blocked"]
    assert account.windows[2].scope == {"model": "Fable"}
    assert usable_for(account, "Opus")
    assert not usable_for(account, "Fable")
    account.windows[0].status = "blocked"
    assert not usable_for(account, "Opus")
    assert json.loads(json.dumps(account.to_dict()))["windows"][0]["resets_at"].endswith("+00:00")


def test_codex_golden():
    account = parse_codex_usage(CODEX)
    assert account.status == "ok"
    assert account.plan == "pro"
    assert [w.window_seconds for w in account.windows] == [18000, 604800, 18000]
    assert account.windows[2].scope == {"model": "test-model", "bucket": "test-bucket"}
    assert not usable_for(account, "test-model")
    assert usable_for(account, "other-model")
    json.dumps(account.to_dict(), allow_nan=False)


def test_weekly_only():
    account = parse_codex_usage({"rate_limit": {"primary_window": None, "secondary_window": SECONDARY}})
    assert account.status == "ok"
    assert len(account.windows) == 1
    assert account.windows[0].kind == "weekly"


@pytest.mark.parametrize("model", [None, "test-bucket", "other-model"])
def test_codex_bucket_only_block_does_not_block_model(model):
    payload = copy.deepcopy(CODEX)
    del payload["additional_rate_limits"][0]["model"]
    account = parse_codex_usage(payload)
    assert account.status == "ok"
    assert account.windows[2].scope == {"bucket": "test-bucket"}
    assert account.windows[2].status == "blocked"
    assert usable_for(account, model)
    account.windows[0].status = "blocked"
    assert not usable_for(account, model)


@pytest.mark.parametrize("fetch,parse,peek,payload", PROVIDERS)
@pytest.mark.parametrize("bad", [None, {}, [], "garbage", {"limits": []}, {"rate_limit": {}},
                                  {"limits": [None]}, {"rate_limit": {"primary_window": "bad"}}])
def test_bad_payloads(fetch, parse, peek, payload, bad, caplog):
    for _ in range(2):
        account = parse(bad)
        assert account.status == "unknown"
        assert account.error
        assert usable_for(account, "anything")
    assert len(caplog.records) == 1


@pytest.mark.parametrize("fetch,parse,peek,payload", PROVIDERS)
@pytest.mark.parametrize("mode", ["html", "empty", "transport", "ok", "429", "401", "rotated", "second401"])
def test_fetch_paths(monkeypatch, fetch, parse, peek, payload, mode):
    creds = oauth.OAuthAccessToken("fake-access", "fake-account", "instance_store")
    newer = oauth.OAuthAccessToken("fake-new-access", "fake-account", "instance_store")
    reader = Mock(side_effect=[creds, newer if mode in {"rotated", "second401"} else creds])
    monkeypatch.setattr(oauth, peek, reader)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["Authorization"].startswith("Bearer fake-")
        assert request.extensions["timeout"]["read"] <= 10
        if peek == "peek_codex_access_token":
            assert request.headers["ChatGPT-Account-Id"] == "fake-account"
        else:
            assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        if mode == "transport":
            raise RuntimeError("secret transport detail")
        if mode in {"html", "empty"}:
            return httpx.Response(200, text="<html>bad</html>" if mode == "html" else "")
        if mode == "429":
            return httpx.Response(429)
        if mode in {"401", "second401"} or (mode == "rotated" and len(calls) == 1):
            return httpx.Response(401)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        account = fetch(http=client)
    assert account.status == ("ok" if mode in {"ok", "rotated"} else "unknown")
    assert account.account_key == "fake-account"
    assert reader.call_count == (2 if mode in {"401", "rotated", "second401"} else 1)
    assert len(calls) == (2 if mode in {"rotated", "second401"} else 1)
    assert account.retry_after == (300 if mode == "429" else None)
    assert "secret" not in json.dumps(account.to_dict())


@pytest.mark.parametrize("parse,payload", [(parse_anthropic_usage, CLAUDE), (parse_codex_usage, CODEX)])
@pytest.mark.parametrize("value", [None, "80", True, float("nan"), float("inf"), {}, []])
def test_mistyped_percent(parse, payload, value):
    payload = copy.deepcopy(payload)
    if "limits" in payload:
        payload["limits"][0]["percent"] = value
    else:
        payload["rate_limit"]["primary_window"]["used_percent"] = value
    assert parse(payload).status == "unknown"


@pytest.mark.parametrize("pct,severity,status", [(79, None, "ok"), (80, None, "warning"),
    (100, None, "blocked"), (1, "blocked", "blocked"), (1, "warning", "warning")])
def test_classify(pct, severity, status):
    assert classify(pct, severity) == status


def test_peek_claude_order(monkeypatch):
    monkeypatch.setattr(oauth, "is_disconnected", lambda *args: False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-env")
    store = Mock(return_value={"access_token": "fake-store", "expires_at": 0, "account_uuid": "fake-org"})
    file = Mock(return_value={"claudeAiOauth": {"accessToken": "fake-file", "expiresAt": 0,
                                               "account": {"uuid": "fake-file-org"}}})
    keychain = Mock(return_value={"claudeAiOauth": {"accessToken": "fake-keychain"}})
    monkeypatch.setattr(oauth, "_read_anthropic_store", store)
    monkeypatch.setattr(oauth, "_read_claude_credentials_file", file)
    monkeypatch.setattr(oauth, "_read_claude_keychain", keychain)
    assert oauth.peek_anthropic_access_token().access_token == "fake-env"
    store.assert_not_called()
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert oauth.peek_anthropic_access_token().account_id == "fake-org"
    file.assert_not_called()
    store.return_value = None
    assert oauth.peek_anthropic_access_token().account_id == "fake-file-org"
    keychain.assert_not_called()
    file.return_value = None
    assert oauth.peek_anthropic_access_token().source == "keychain"
    keychain.return_value = None
    assert oauth.peek_anthropic_access_token() is None
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-env")
    monkeypatch.setattr(oauth, "is_disconnected", lambda *args: True)
    assert oauth.peek_anthropic_access_token() is None


@pytest.mark.platform_sensitive
def test_peek_codex(monkeypatch, tmp_path):
    # Drive the REAL _codex_store_path(instance_paths()) resolution so a regression to a
    # no-arg call raises TypeError -> swallowed -> None and this test fails, rather than
    # being masked by a zero-arg stub.
    monkeypatch.setattr(oauth, "is_disconnected", lambda *args: False)
    paths = InstancePaths(instance_id="test", box_root=tmp_path / "box",
                          instance_root=tmp_path / "inst", app_root=tmp_path / "app")
    monkeypatch.setattr(oauth, "instance_paths", lambda: paths)
    paths.config_dir.mkdir(parents=True)
    path = paths.config_dir / "codex-oauth.json"
    assert oauth.peek_codex_access_token() is None
    path.write_text(json.dumps({"tokens": {"access_token": "fake-expired", "account_id": "fake-account"}}))
    before = path.read_bytes()
    assert oauth.peek_codex_access_token().account_id == "fake-account"
    assert path.read_bytes() == before
    monkeypatch.setattr(oauth, "is_disconnected", lambda *args: True)
    assert oauth.peek_codex_access_token() is None


@pytest.mark.parametrize("fetch,parse,peek,payload", PROVIDERS)
def test_absent_credentials_do_not_request(monkeypatch, fetch, parse, peek, payload):
    monkeypatch.setattr(oauth, peek, lambda: None)
    http = Mock()
    assert fetch(http=http).status == "unknown"
    http.get.assert_not_called()


@pytest.mark.parametrize("fetch,parse,peek,payload", PROVIDERS)
def test_credential_read_error_is_unknown(monkeypatch, fetch, parse, peek, payload, caplog):
    monkeypatch.setattr(oauth, peek, Mock(side_effect=RuntimeError("private detail")))
    for _ in range(2):
        assert fetch(http=Mock()).status == "unknown"
    assert len(caplog.records) == 1
    assert "private detail" not in caplog.text


@pytest.mark.parametrize("parse,payload", [(parse_anthropic_usage, CLAUDE), (parse_codex_usage, CODEX)])
@pytest.mark.parametrize("field,value", [("reset", "not-a-date"), ("reset", True),
    ("reset", "2026-10-23T12:00:00"), ("scope", []), ("scope", {"model": 123})])
def test_schema_drift(parse, payload, field, value):
    payload = copy.deepcopy(payload)
    if "limits" in payload:
        target = payload["limits"][0]
        target["resets_at" if field == "reset" else field] = value
    elif field == "reset":
        payload["rate_limit"]["primary_window"]["reset_at"] = value
    else:
        payload["additional_rate_limits"][0]["scope"] = value
    assert parse(payload).status == "unknown"


@pytest.mark.platform_sensitive
def test_peek_claude_store_is_read_only(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth, "is_disconnected", lambda *args: False)
    path = tmp_path / "anthropic.json"
    monkeypatch.setattr(oauth, "_anthropic_store_path", lambda: path)
    path.write_text(json.dumps({"access_token": "fake-old-access", "expires_at": 0,
                                "refresh_token": "fake-unused-refresh", "account_uuid": "fake-org"}))
    before = path.read_bytes()
    assert oauth.peek_anthropic_access_token().account_id == "fake-org"
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("parse,payload", [(parse_anthropic_usage, CLAUDE), (parse_codex_usage, CODEX)])
@pytest.mark.parametrize("severity,status,expected", [("ok", "blocked", "blocked"),
                                                     (None, "warning", "warning"),
                                                     (123, "ok", "unknown")])
def test_provider_status(parse, payload, severity, status, expected):
    payload = copy.deepcopy(payload)
    node = payload["limits"][0] if "limits" in payload else payload["rate_limit"]["primary_window"]
    node.update(severity=severity, status=status)
    account = parse(payload)
    if expected == "unknown":
        assert account.status == "unknown"
    else:
        assert account.windows[0].status == expected


# Live payload shapes (2026-10-23), values redacted; the hand-built spike shapes above
# didn't match either provider, so both read "unrecognized usage payload" in production.
CLAUDE_LIVE = {
    "five_hour": {"utilization": 5.0, "resets_at": "2026-10-23T11:19:59.759401+00:00"},
    "seven_day": {"utilization": 14.0, "resets_at": "2026-10-29T09:59:59.759425+00:00"},
    "extra_usage": {"is_enabled": False, "used_credits": 0.0, "currency": "USD"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 5, "severity": "normal",
         "resets_at": "2026-10-23T11:19:59.759401+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 14, "severity": "normal",
         "resets_at": "2026-10-29T09:59:59.759425+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
         "resets_at": "2026-10-29T10:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False},
    ],
}
CODEX_LIVE = {
    "plan_type": "prolite",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"used_percent": 50, "limit_window_seconds": 604800,
                                      "reset_after_seconds": 553560, "reset_at": 1793296874},
                   "secondary_window": None},
    "code_review_rate_limit": None,
    "additional_rate_limits": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}


def test_claude_live_payload():
    account = parse_anthropic_usage(copy.deepcopy(CLAUDE_LIVE))
    assert account.status == "ok"
    assert [w.kind for w in account.windows] == ["session", "weekly", "weekly"]
    assert [w.window_seconds for w in account.windows] == [18000, 604800, 604800]
    assert [w.used_pct for w in account.windows] == [5, 14, 0]
    assert [w.binding for w in account.windows] == [False, True, False]
    assert account.windows[2].scope == {"model": "Fable"}
    assert all(w.status == "ok" for w in account.windows)


def test_claude_scope_prefers_model_id():
    payload = copy.deepcopy(CLAUDE_LIVE)
    payload["limits"][2]["scope"]["model"]["id"] = "claude-fable-5-1"
    assert parse_anthropic_usage(payload).windows[2].scope == {"model": "claude-fable-5-1"}


def test_codex_live_payload():
    account = parse_codex_usage(copy.deepcopy(CODEX_LIVE))
    assert account.status == "ok"
    assert account.plan == "prolite"
    assert [(w.kind, w.used_pct) for w in account.windows] == [("weekly", 50)]
