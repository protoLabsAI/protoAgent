"""Unit tests for graph.middleware.redaction module."""


from graph.middleware.redaction import redact, PATTERNS


# ---------------------------------------------------------------------------
# Pattern presence
# ---------------------------------------------------------------------------

def test_patterns_dict_has_required_keys():
    assert "bearer_token" in PATTERNS
    assert "openai_key" in PATTERNS
    assert "generic_api_key" in PATTERNS
    assert "env_var_assignment" in PATTERNS
    assert len(PATTERNS) >= 4


# ---------------------------------------------------------------------------
# Bearer token
# ---------------------------------------------------------------------------

def test_bearer_token_in_header_string():
    s = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
    result = redact(s)
    assert "eyJhbGciOiJIUzI1NiJ9" not in result
    assert "[REDACTED]" in result
    assert "Authorization" in result
    assert "Bearer" in result


def test_bearer_token_case_insensitive():
    s = "authorization: bearer mysecrettoken12345"
    result = redact(s)
    assert "mysecrettoken12345" not in result
    assert "[REDACTED]" in result


def test_bearer_token_in_dict_value():
    data = {"headers": "Authorization: Bearer abc123token999"}
    result = redact(data)
    assert "abc123token999" not in result["headers"]
    assert "[REDACTED]" in result["headers"]


# ---------------------------------------------------------------------------
# OpenAI-style API keys (sk-...)
# ---------------------------------------------------------------------------

def test_openai_key_bare_string():
    s = "sk-aBcDefGhIjKlMnOpQrStUvWxYz1234"
    result = redact(s)
    assert "sk-aBcDefGhIjKlMnOpQrStUvWxYz1234" not in result
    assert "[REDACTED]" in result


def test_openai_key_in_sentence():
    s = "my key is sk-proj-1234567890abcdefghij and nothing else"
    result = redact(s)
    assert "sk-proj-1234567890abcdefghij" not in result
    assert "[REDACTED]" in result


def test_openai_key_in_dict_value():
    data = {"openai_token": "sk-testKey0000000000000000000000"}
    result = redact(data)
    # The dict key is not a sensitive env key, so value redaction via pattern
    assert "sk-testKey0000000000000000000000" not in str(result)
    assert "[REDACTED]" in str(result)


def test_openai_key_too_short_not_redacted():
    # Keys shorter than 20 chars after sk- should not be redacted by openai pattern
    s = "sk-short"
    result = redact(s)
    # Should remain unchanged (not matching the pattern)
    assert result == s


# ---------------------------------------------------------------------------
# Generic api_key
# ---------------------------------------------------------------------------

def test_generic_api_key_equals():
    s = 'api_key="abcdefghijklmnopq1234"'
    result = redact(s)
    assert "abcdefghijklmnopq1234" not in result
    assert "[REDACTED]" in result


def test_generic_api_key_colon():
    s = "api_key: xyzXYZ123456789012"
    result = redact(s)
    assert "xyzXYZ123456789012" not in result
    assert "[REDACTED]" in result


def test_apikey_no_separator():
    s = 'apikey="supersecretvalue1234"'
    result = redact(s)
    assert "supersecretvalue1234" not in result
    assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Environment variable dict key redaction
# ---------------------------------------------------------------------------

def test_env_var_redaction_openai():
    data = {"OPENAI_API_KEY": "sk-real-secret-key-that-is-long"}
    result = redact(data)
    assert result["OPENAI_API_KEY"] == "[REDACTED]"


def test_env_var_redaction_langfuse_secret():
    data = {"LANGFUSE_SECRET_KEY": "lf_secret_1234567890abcdef"}
    result = redact(data)
    assert result["LANGFUSE_SECRET_KEY"] == "[REDACTED]"


def test_env_var_redaction_a2a_auth_token():
    data = {"A2A_AUTH_TOKEN": "bearer-token-value-here"}
    result = redact(data)
    assert result["A2A_AUTH_TOKEN"] == "[REDACTED]"


def test_env_var_lowercase_api_key():
    data = {"api_key": "my_secret_api_key_12345"}
    result = redact(data)
    assert result["api_key"] == "[REDACTED]"


def test_env_var_authorization_header():
    data = {"authorization": "Bearer some-jwt-token"}
    result = redact(data)
    assert result["authorization"] == "[REDACTED]"


def test_non_sensitive_key_unchanged():
    data = {"url": "https://api.example.com/v1/query", "count": 42}
    result = redact(data)
    assert result["url"] == "https://api.example.com/v1/query"
    assert result["count"] == 42


# ---------------------------------------------------------------------------
# Nested dict traversal
# ---------------------------------------------------------------------------

def test_nested_dict():
    data = {
        "outer": {
            "inner": {
                "OPENAI_API_KEY": "secret-key-value-here"
            }
        }
    }
    result = redact(data)
    assert result["outer"]["inner"]["OPENAI_API_KEY"] == "[REDACTED]"


def test_nested_list():
    data = {
        "headers": [
            "Authorization: Bearer token123456789012345",
            "Content-Type: application/json",
        ]
    }
    result = redact(data)
    assert "token123456789012345" not in result["headers"][0]
    assert "[REDACTED]" in result["headers"][0]
    assert result["headers"][1] == "Content-Type: application/json"


def test_deeply_nested():
    data = {"a": {"b": {"c": {"d": {"OPENAI_API_KEY": "deep-secret-value"}}}}}
    result = redact(data)
    assert result["a"]["b"]["c"]["d"]["OPENAI_API_KEY"] == "[REDACTED]"


def test_list_of_dicts():
    data = [
        {"OPENAI_API_KEY": "key1value1234567890"},
        {"url": "https://example.com"},
        {"Authorization": "Bearer tokenvalue12345"},
    ]
    result = redact(data)
    assert result[0]["OPENAI_API_KEY"] == "[REDACTED]"
    assert result[1]["url"] == "https://example.com"
    assert result[2]["Authorization"] == "[REDACTED]"


def test_mixed_types_in_list():
    data = ["Authorization: Bearer tok12345678901234", 42, None, True]
    result = redact(data)
    assert "tok12345678901234" not in result[0]
    assert "[REDACTED]" in result[0]
    assert result[1] == 42
    assert result[2] is None
    assert result[3] is True


# ---------------------------------------------------------------------------
# False positives — legitimate data must not be mangled
# ---------------------------------------------------------------------------

def test_false_positives_url_with_key():
    # URL containing 'key' should not be redacted (no credential pattern match)
    url = "https://maps.example.com/api/v1/geocode?format=json"
    result = redact(url)
    assert result == url


def test_false_positives_short_values():
    s = "the key is important"
    result = redact(s)
    assert result == s


def test_false_positives_numeric_values():
    data = {"count": 100, "page": 2, "limit": 50}
    result = redact(data)
    assert result == data


def test_false_positives_normal_text():
    s = "The query returned 42 results for the search term."
    result = redact(s)
    assert result == s


def test_false_positives_content_type_header():
    s = "Content-Type: application/json"
    result = redact(s)
    assert result == s


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_string():
    assert redact("") == ""


def test_empty_dict():
    assert redact({}) == {}


def test_empty_list():
    assert redact([]) == []


def test_none_value():
    assert redact(None) is None


def test_integer_value():
    assert redact(42) == 42


def test_bool_value():
    assert redact(True) is True


def test_nested_redaction():
    data = {
        "tool_args": {
            "query": "weather in Paris",
            "api_key": "supersecretvalue12345",
        },
        "headers": {
            "Authorization": "Bearer jwt.token.value123",
            "Accept": "application/json",
        },
    }
    result = redact(data)
    assert result["tool_args"]["query"] == "weather in Paris"
    assert result["tool_args"]["api_key"] == "[REDACTED]"
    assert result["headers"]["Authorization"] == "[REDACTED]"
    assert result["headers"]["Accept"] == "application/json"


def test_langfuse_redaction():
    # Simulate a Langfuse span payload
    payload = {
        "name": "tool:fetch_secret",
        "input": {
            "LANGFUSE_SECRET_KEY": "lf_sk_abc123def456",
            "query": "get config",
        },
        "output": "Authorization: Bearer mytoken1234567890",
    }
    result = redact(payload)
    assert result["input"]["LANGFUSE_SECRET_KEY"] == "[REDACTED]"
    assert result["input"]["query"] == "get config"
    assert "mytoken1234567890" not in result["output"]
    assert "[REDACTED]" in result["output"]
