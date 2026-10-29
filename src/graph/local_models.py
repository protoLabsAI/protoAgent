"""Discover well-known local OpenAI-compatible model servers."""

from graph.config_io import list_gateway_models


_ENDPOINTS = (
    {"id": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434/v1"},
    {"id": "lmstudio", "label": "LM Studio", "base_url": "http://127.0.0.1:1234/v1"},
)


def detect_local_models(timeout: float = 1.5) -> list[dict]:
    """Probe both local servers with a short per-endpoint timeout, without raising."""
    endpoints = []
    for endpoint in _ENDPOINTS:
        try:
            models, error = list_gateway_models(
                endpoint["base_url"], "", timeout=timeout, allow_env_key=False
            )
            found = not error
            if found and not models:
                error = "running, but no models pulled"
        except Exception as exc:
            models, error, found = [], f"probe failed ({type(exc).__name__})", False
        endpoints.append({**endpoint, "found": found, "models": models, "error": error})
    return endpoints
