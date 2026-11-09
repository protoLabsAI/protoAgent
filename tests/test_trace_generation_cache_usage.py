"""``trace_generation`` maps LangChain's nested cache counts into Langfuse usage.

The fleet generation is fed ``AIMessage.usage_metadata``, where cache reads and
writes are nested under ``input_token_details`` and are subsets of
``input_tokens``. Before this fix only the flat ``cache_read_input_tokens`` name was
read, so every cached prompt reached Langfuse as uncached input.
"""

from unittest.mock import MagicMock

import pytest

from observability import tracing


@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return fake


def _usage_details(fake):
    assert fake.start_observation.call_count == 1
    return fake.start_observation.call_args.kwargs["usage_details"]


def test_nested_langchain_cache_counts_are_reported(fake_langfuse):
    usage = {
        "input_tokens": 46_000,
        "output_tokens": 300,
        "total_tokens": 46_300,
        "input_token_details": {"cache_read": 44_000, "cache_creation": 1_700},
    }
    tracing.trace_generation(name="t", model="claude-sonnet-5-5", usage=usage)
    assert _usage_details(fake_langfuse) == {
        "input": 300,
        "output": 300,
        "input_cache_read": 44_000,
        "input_cache_creation": 1_700,
        "total": 46_300,
    }


def test_flat_telemetry_names_pass_through_unchanged(fake_langfuse):
    # The ACP coder's shape: input_tokens already excludes cache, so no subtraction.
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 30,
    }
    tracing.trace_generation(name="t", usage=usage)
    assert _usage_details(fake_langfuse) == {
        "input": 100,
        "output": 50,
        "input_cache_read": 700,
        "input_cache_creation": 30,
    }


def test_service_tier_prefixed_cache_read(fake_langfuse):
    usage = {"input_tokens": 500, "output_tokens": 5, "input_token_details": {"priority_cache_read": 400}}
    tracing.trace_generation(name="t", usage=usage)
    assert _usage_details(fake_langfuse) == {"input": 100, "output": 5, "input_cache_read": 400}


def test_uncached_usage_unchanged(fake_langfuse):
    usage = {"input_tokens": 2_000, "output_tokens": 50, "total_tokens": 2_050}
    tracing.trace_generation(name="t", usage=usage)
    assert _usage_details(fake_langfuse) == {"input": 2_000, "output": 50, "total": 2_050}


def test_fully_cached_prompt_omits_zero_input(fake_langfuse):
    usage = {"input_tokens": 800, "output_tokens": 5, "input_token_details": {"cache_read": 800}}
    tracing.trace_generation(name="t", usage=usage)
    assert _usage_details(fake_langfuse) == {"output": 5, "input_cache_read": 800}
