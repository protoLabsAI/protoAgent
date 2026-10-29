"""Local model discovery and its config route, without live network calls."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from graph import local_models
# Load the registrar before tests replace config_io with narrow module fakes.
from operator_api.config_routes import register_config_routes


@pytest.mark.parametrize(
    "models,error,found",
    [
        (["local-model"], "", True),
        ([], "", True),
        ([], "connection refused", False),
        ([], "connection timed out", False),
    ],
)
def test_detect_local_models(monkeypatch, models, error, found):
    calls = []

    def probe(base, key, *, timeout, allow_env_key):
        calls.append((base, key, timeout, allow_env_key))
        return models, error

    monkeypatch.setattr(local_models, "list_gateway_models", probe)
    endpoints = local_models.detect_local_models()

    assert endpoints == [
        {
            "id": ident,
            "label": label,
            "base_url": base,
            "found": found,
            "models": models,
            "error": error or ("running, but no models pulled" if not models else ""),
        }
        for ident, label, base in (
            ("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
            ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1"),
        )
    ]
    assert calls == [(row["base_url"], "", 1.5, False) for row in endpoints]


def test_raising_probe_does_not_skip_other_endpoint(monkeypatch):
    def probe(base, key, *, timeout, allow_env_key):
        assert timeout == 0.25
        if ":11434/" in base:
            raise RuntimeError("unexpected failure with sensitive details")
        return ["working-model"], ""

    monkeypatch.setattr(local_models, "list_gateway_models", probe)
    first, second = local_models.detect_local_models(timeout=0.25)
    assert first["found"] is False
    assert first["models"] == []
    assert first["error"] == "probe failed (RuntimeError)"
    assert second["found"] is True
    assert second["models"] == ["working-model"]
    assert second["error"] == ""


def test_local_models_route_runs_in_worker_thread(monkeypatch):
    endpoints = [{"id": "ollama", "found": False, "models": [], "error": "refused"}]

    def detect():
        # A synchronous probe must run outside the request's running event loop.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return endpoints

    monkeypatch.setattr(local_models, "detect_local_models", detect)
    app = FastAPI()
    register_config_routes(app)
    with TestClient(app) as client:
        response = client.post("/api/config/local-models")
    assert response.status_code == 200
    assert response.json() == {"endpoints": endpoints}
