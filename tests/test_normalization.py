"""Tests for devils_advocate.normalization — LLM-based response normalization."""

from __future__ import annotations

import httpx
import pytest
import respx

from devils_advocate.normalization import normalize_review_response
from devils_advocate.prompts import build_normalization_prompt
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_model(monkeypatch):
    """Return a ModelConfig using the Anthropic provider with a fake key."""
    monkeypatch.setenv("TEST_API_KEY", "fake-key")
    return ModelConfig(
        name="test-model",
        provider="anthropic",
        model_id="test-id",
        api_key_env="TEST_API_KEY",
    )


STRUCTURED_REVIEW_TEXT = """\
REVIEW POINT 1:
SEVERITY: high
CATEGORY: security
DESCRIPTION: SQL injection vulnerability in user input handling
RECOMMENDATION: Use parameterized queries
LOCATION: app.py line 42

REVIEW POINT 2:
SEVERITY: medium
CATEGORY: performance
DESCRIPTION: N+1 query pattern in the dashboard endpoint
RECOMMENDATION: Use eager loading or batch queries
LOCATION: views.py line 88
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_successful_normalization(anthropic_model):
    """Mock a successful Anthropic response returning structured review points."""
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": STRUCTURED_REVIEW_TEXT}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )
        )

        async with httpx.AsyncClient() as client:
            points = await normalize_review_response(
                client,
                raw="Some unstructured review text here.",
                model=anthropic_model,
                reviewer_name="reviewer-1",
            )

        assert len(points) == 2
        assert points[0].severity == "high"
        assert points[0].category == "security"
        assert "SQL injection" in points[0].description
        assert points[0].reviewer == "reviewer-1"
        assert points[1].severity == "medium"
        assert points[1].category == "performance"


async def test_api_error_returns_empty_list(anthropic_model):
    """When the API returns a server error, normalization returns an empty list."""
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        log_messages = []

        async with httpx.AsyncClient() as client:
            points = await normalize_review_response(
                client,
                raw="Some review text",
                model=anthropic_model,
                reviewer_name="reviewer-1",
                log_fn=log_messages.append,
            )

        assert points == []
        # The log should indicate normalization was attempted and failed
        assert any("Normalization" in msg for msg in log_messages)


async def test_normalization_prompt_contains_raw_text(anthropic_model):
    """Verify that build_normalization_prompt embeds the raw review text."""
    raw_text = "This is the raw unstructured review output from a model."
    prompt = build_normalization_prompt(raw_text)

    # The prompt should contain the raw text between the delimiters
    assert raw_text in prompt
    assert "=== RAW REVIEW RESPONSE ===" in prompt
    assert "=== END RAW REVIEW RESPONSE ===" in prompt


async def test_normalization_with_start_index(anthropic_model):
    """Points should use IDs starting from the given start_index."""
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": STRUCTURED_REVIEW_TEXT}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )
        )

        async with httpx.AsyncClient() as client:
            points = await normalize_review_response(
                client,
                raw="unstructured",
                model=anthropic_model,
                reviewer_name="rev",
                start_index=10,
            )

        assert len(points) == 2
        # Points should be numbered from start_index + 1
        assert points[0].point_id == "temp_011"
        assert points[1].point_id == "temp_012"


async def test_log_fn_called_on_success(anthropic_model):
    """The log_fn callback is invoked when normalization is attempted."""
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": STRUCTURED_REVIEW_TEXT}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )
        )

        log_messages = []

        async with httpx.AsyncClient() as client:
            await normalize_review_response(
                client,
                raw="unstructured",
                model=anthropic_model,
                reviewer_name="rev-x",
                log_fn=log_messages.append,
            )

        assert any("Normalization: calling" in msg for msg in log_messages)
        assert any("rev-x" in msg for msg in log_messages)
