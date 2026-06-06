"""validate_model_connection — auth-error sanitization.

A gateway (e.g. LiteLLM) dumps the masked key, a token *hash*, and internal
table names into a 401 body. That must never reach the setup UI verbatim — the
error is shown to the operator and echoing a token/hash is a leak + useless.
"""

from __future__ import annotations


from graph import config_io


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_auth_error_strips_token_hash_and_key(monkeypatch):
    # The exact leaky shape the proto-labs LiteLLM gateway returns for a bad key.
    leaky = (
        "Authentication Error, Invalid proxy server token passed. "
        "Received API Key = sk-...-xxx, Key Hash (Token) =b4cbf50b702d5431ff6, "
        "Unable to find token in cache or `LiteLLM_VerificationTokenTable`"
    )
    import httpx
    monkeypatch.setattr(httpx, "Client", _client_returning(_FakeResp(401, {"error": {"message": leaky}})))

    ok, err = config_io.validate_model_connection("https://gw.example/v1", "sk-bogus", "m")
    assert ok is False
    # The actionable cause survives…
    assert "Authentication Error" in err
    # …but nothing secret/internal leaks.
    for leak in ("Key Hash", "sk-...-xxx", "LiteLLM_VerificationTokenTable", "Unable to find token"):
        assert leak not in err, f"leaked {leak!r} in {err!r}"


def test_clean_gateway_message_passes_through(monkeypatch):
    import httpx
    monkeypatch.setattr(
        httpx, "Client",
        _client_returning(_FakeResp(400, {"error": {"message": "expected to start with 'sk-'"}})),
    )
    ok, err = config_io.validate_model_connection("https://gw.example/v1", "bad", "m")
    assert ok is False
    assert "expected to start with 'sk-'" in err


def _client_returning(resp: _FakeResp):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return resp

    return _FakeClient
