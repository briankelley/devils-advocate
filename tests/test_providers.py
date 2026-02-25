"""Tests for devils_advocate.providers — LLM API provider calls and retry logic."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from devils_advocate.providers import (
    ANTHROPIC_API_URL,
    DEFAULT_MAX_RETRIES,
    MAX_OUTPUT_TOKENS,
    _ANTHROPIC_THINKING_BUDGETS,
    call_anthropic,
    call_minimax,
    call_model,
    call_openai_compatible,
    call_openai_responses,
    call_with_retry,
)
from devils_advocate.types import APIError, ModelConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model(
    name="test-model",
    provider="anthropic",
    model_id="claude-sonnet-4-20250514",
    api_key_env="TEST_API_KEY",
    api_base="",
    thinking=False,
    timeout=120,
    use_completion_tokens=False,
    use_responses_api=False,
):
    """Build a ModelConfig for testing."""
    return ModelConfig(
        name=name,
        provider=provider,
        model_id=model_id,
        api_key_env=api_key_env,
        api_base=api_base,
        thinking=thinking,
        timeout=timeout,
        use_completion_tokens=use_completion_tokens,
        use_responses_api=use_responses_api,
    )


def _anthropic_response(text="Hello", input_tokens=100, output_tokens=50):
    """Build a mock Anthropic Messages API response body."""
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _openai_response(text="Hello", prompt_tokens=100, completion_tokens=50):
    """Build a mock OpenAI chat/completions response body."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _minimax_response(text="Hello", prompt_tokens=100, completion_tokens=50):
    """Build a mock MiniMax chatcompletion_v2 response body."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    """Ensure TEST_API_KEY is available for all tests."""
    monkeypatch.setenv("TEST_API_KEY", "fake-key-for-testing")


# ===========================================================================
# call_anthropic
# ===========================================================================


class TestCallAnthropic:
    """Tests for call_anthropic — Anthropic Messages API construction."""

    async def test_basic_successful_call(self):
        """A standard call with no thinking returns text and usage."""
        model = _make_model()
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response("Review output"))
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_anthropic(
                    client, model, "You are a reviewer.", "Review this code."
                )

        assert text == "Review output"
        assert usage == {"input_tokens": 100, "output_tokens": 50}

    async def test_system_prompt_included_in_body(self):
        """When system_prompt is non-empty, it appears in the request body."""
        model = _make_model()
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "Be critical.", "Check this.")

        body = route.calls.last.request.content
        import json
        parsed = json.loads(body)
        assert parsed["system"] == "Be critical."

    async def test_empty_system_prompt_omitted(self):
        """When system_prompt is empty, 'system' key is not in the request body."""
        model = _make_model()
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "", "Check this.")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "system" not in parsed

    async def test_correct_headers_sent(self):
        """Verify the required Anthropic headers are set."""
        model = _make_model()
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "sys", "usr")

        headers = route.calls.last.request.headers
        assert headers["x-api-key"] == "fake-key-for-testing"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"

    async def test_model_id_and_max_tokens_passed(self):
        """The model_id and max_tokens appear in the request body."""
        model = _make_model(model_id="claude-haiku-test")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "sys", "usr", max_tokens=8192)

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["model"] == "claude-haiku-test"
        assert parsed["max_tokens"] == 8192

    async def test_thinking_adaptive_for_opus_4_6(self):
        """Models containing 'opus-4-6' use adaptive thinking."""
        model = _make_model(model_id="claude-opus-4-6-20260101", thinking=True)
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["thinking"] == {"type": "adaptive"}
        # max_tokens should NOT be inflated for adaptive mode
        assert parsed["max_tokens"] == MAX_OUTPUT_TOKENS

    async def test_thinking_adaptive_for_sonnet_4_6(self):
        """Models containing 'sonnet-4-6' use adaptive thinking."""
        model = _make_model(model_id="claude-sonnet-4-6-20260101", thinking=True)
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["thinking"] == {"type": "adaptive"}

    async def test_thinking_budget_for_non_adaptive_model(self):
        """Non-opus-4-6/sonnet-4-6 models use budget-based thinking."""
        model = _make_model(model_id="claude-sonnet-4-20250514", thinking=True)
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(
                    client, model, "sys", "usr", max_tokens=MAX_OUTPUT_TOKENS, mode="plan"
                )

        import json
        parsed = json.loads(route.calls.last.request.content)
        budget = _ANTHROPIC_THINKING_BUDGETS["plan"]
        assert parsed["thinking"] == {"type": "enabled", "budget_tokens": budget}
        # max_tokens inflated by budget
        assert parsed["max_tokens"] == MAX_OUTPUT_TOKENS + budget

    async def test_thinking_budget_default_for_unknown_mode(self):
        """Unknown mode falls back to default budget (8192)."""
        model = _make_model(model_id="claude-sonnet-4-20250514", thinking=True)
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(
                    client, model, "sys", "usr", max_tokens=MAX_OUTPUT_TOKENS, mode="unknown_mode"
                )

        import json
        parsed = json.loads(route.calls.last.request.content)
        # .get(mode, 8192) falls back to 8192 for unknown mode
        assert parsed["thinking"]["budget_tokens"] == 8192
        assert parsed["max_tokens"] == MAX_OUTPUT_TOKENS + 8192

    async def test_thinking_budget_per_mode(self):
        """Each known mode produces the correct budget from the lookup table."""
        for mode, expected_budget in _ANTHROPIC_THINKING_BUDGETS.items():
            model = _make_model(model_id="claude-sonnet-4-20250514", thinking=True)
            with respx.mock:
                route = respx.post(ANTHROPIC_API_URL).mock(
                    return_value=httpx.Response(200, json=_anthropic_response())
                )
                async with httpx.AsyncClient() as client:
                    await call_anthropic(
                        client, model, "sys", "usr", max_tokens=1000, mode=mode
                    )

            import json
            parsed = json.loads(route.calls.last.request.content)
            assert parsed["thinking"]["budget_tokens"] == expected_budget, (
                f"Budget mismatch for mode={mode!r}"
            )
            assert parsed["max_tokens"] == 1000 + expected_budget

    async def test_no_thinking_when_disabled(self):
        """When thinking=False, no thinking key appears in the body."""
        model = _make_model(thinking=False)
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_anthropic(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "thinking" not in parsed

    async def test_multiple_text_blocks_concatenated(self):
        """Multiple text blocks in the response are concatenated."""
        response_body = {
            "content": [
                {"type": "thinking", "thinking": "internal thought"},
                {"type": "text", "text": "Part one. "},
                {"type": "text", "text": "Part two."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        model = _make_model()
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_anthropic(client, model, "sys", "usr")

        assert text == "Part one. Part two."
        assert usage == {"input_tokens": 10, "output_tokens": 20}

    async def test_non_text_blocks_ignored(self):
        """Blocks with type != 'text' (e.g. thinking) are ignored."""
        response_body = {
            "content": [
                {"type": "thinking", "thinking": "..."},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        model = _make_model()
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_anthropic(client, model, "sys", "usr")

        assert text == ""

    async def test_http_error_propagates(self):
        """HTTP errors raise httpx.HTTPStatusError."""
        model = _make_model()
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(500, text="Server Error")
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await call_anthropic(client, model, "sys", "usr")


# ===========================================================================
# call_openai_compatible
# ===========================================================================


class TestCallOpenAICompatible:
    """Tests for call_openai_compatible — OpenAI-compatible chat completions."""

    async def test_basic_successful_call(self):
        """Standard call returns text and usage."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response("The answer"))
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_openai_compatible(
                    client, model, "You are helpful.", "What is 2+2?"
                )

        assert text == "The answer"
        assert usage == {"input_tokens": 100, "output_tokens": 50}

    async def test_system_prompt_in_messages(self):
        """Non-empty system prompt appears as a system message."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.test.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "Be precise.", "Question")

        import json
        parsed = json.loads(route.calls.last.request.content)
        messages = parsed["messages"]
        assert messages[0] == {"role": "system", "content": "Be precise."}
        assert messages[1] == {"role": "user", "content": "Question"}

    async def test_empty_system_prompt_omitted_from_messages(self):
        """Empty system prompt produces only a user message."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.test.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "", "Question")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert len(parsed["messages"]) == 1
        assert parsed["messages"][0]["role"] == "user"

    async def test_max_completion_tokens_for_o3_model(self):
        """o3-* models use max_completion_tokens instead of max_tokens."""
        model = _make_model(
            provider="openai",
            model_id="o3-mini",
            api_base="https://api.openai.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr", max_tokens=4096)

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["max_completion_tokens"] == 4096
        assert "max_tokens" not in parsed

    async def test_max_completion_tokens_for_o4_model(self):
        """o4-* models use max_completion_tokens instead of max_tokens."""
        model = _make_model(
            provider="openai",
            model_id="o4-mini-2025-04-16",
            api_base="https://api.openai.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "max_completion_tokens" in parsed
        assert "max_tokens" not in parsed

    async def test_use_completion_tokens_flag(self):
        """Models with use_completion_tokens=True use max_completion_tokens."""
        model = _make_model(
            provider="openai",
            model_id="some-custom-model",
            api_base="https://api.custom.com/v1",
            use_completion_tokens=True,
        )
        with respx.mock:
            route = respx.post("https://api.custom.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr", max_tokens=2048)

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["max_completion_tokens"] == 2048
        assert "max_tokens" not in parsed

    async def test_normal_model_uses_max_tokens(self):
        """Non-o3/o4 models without use_completion_tokens use max_tokens."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr", max_tokens=4096)

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["max_tokens"] == 4096
        assert "max_completion_tokens" not in parsed

    async def test_reasoning_effort_for_openai_spec_mode(self):
        """OpenAI models with thinking=True use 'medium' effort for spec mode."""
        model = _make_model(
            provider="openai",
            model_id="o3-mini",
            api_base="https://api.openai.com/v1",
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(
                    client, model, "sys", "usr", mode="spec"
                )

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["reasoning_effort"] == "medium"

    async def test_reasoning_effort_for_openai_non_spec_mode(self):
        """OpenAI models with thinking=True use 'high' effort for non-spec modes."""
        model = _make_model(
            provider="openai",
            model_id="o3-mini",
            api_base="https://api.openai.com/v1",
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(
                    client, model, "sys", "usr", mode="plan"
                )

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["reasoning_effort"] == "high"

    async def test_no_reasoning_effort_for_non_openai_base(self):
        """Non-OpenAI bases do not include reasoning_effort even with thinking=True."""
        model = _make_model(
            provider="openai",
            model_id="some-model",
            api_base="https://api.together.xyz/v1",
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.together.xyz/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr", mode="plan")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "reasoning_effort" not in parsed

    async def test_thinking_for_moonshot_provider(self):
        """Moonshot bases include thinking={type: enabled}."""
        model = _make_model(
            provider="openai",
            model_id="kimi-k2",
            api_base="https://api.moonshot.cn/v1",
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.moonshot.cn/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in parsed

    async def test_no_thinking_params_when_disabled(self):
        """When thinking=False, no reasoning_effort or thinking key appears."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
            thinking=False,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "reasoning_effort" not in parsed
        assert "thinking" not in parsed

    async def test_empty_content_warning_logged(self, caplog):
        """Warns when output_tokens > 0 but visible text is empty."""
        model = _make_model(
            provider="openai",
            model_id="o3-mini",
            api_base="https://api.openai.com/v1",
        )
        response_body = {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 200},
        }
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            with caplog.at_level(logging.WARNING, logger="devils_advocate"):
                async with httpx.AsyncClient() as client:
                    text, usage = await call_openai_compatible(
                        client, model, "sys", "usr", max_tokens=4096
                    )

        assert text == ""
        assert usage["output_tokens"] == 200
        assert any("0 visible content" in record.message for record in caplog.records)

    async def test_no_warning_when_text_present(self, caplog):
        """No warning emitted when content is non-empty."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response("Some text"))
            )
            with caplog.at_level(logging.WARNING, logger="devils_advocate"):
                async with httpx.AsyncClient() as client:
                    await call_openai_compatible(client, model, "sys", "usr")

        assert not any("0 visible content" in record.message for record in caplog.records)

    async def test_no_warning_when_zero_output_tokens(self, caplog):
        """No warning when both content and output_tokens are zero."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
        )
        response_body = {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 0},
        }
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            with caplog.at_level(logging.WARNING, logger="devils_advocate"):
                async with httpx.AsyncClient() as client:
                    await call_openai_compatible(client, model, "sys", "usr")

        assert not any("0 visible content" in record.message for record in caplog.records)

    async def test_none_content_treated_as_empty(self):
        """If message content is None, it is coerced to empty string."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.test.com/v1",
        )
        response_body = {
            "choices": [{"message": {"content": None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        with respx.mock:
            respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_openai_compatible(client, model, "", "hi")

        assert text == ""

    async def test_authorization_header(self):
        """Bearer token is set in the Authorization header."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.test.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr")

        headers = route.calls.last.request.headers
        assert headers["authorization"] == "Bearer fake-key-for-testing"

    async def test_trailing_slash_stripped_from_api_base(self):
        """api_base trailing slashes are stripped before building the URL."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.test.com/v1/",
        )
        with respx.mock:
            route = respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_compatible(client, model, "sys", "usr")

        assert route.called


# ===========================================================================
# call_minimax
# ===========================================================================


class TestCallMinimax:
    """Tests for call_minimax — MiniMax native chatcompletion_v2 API."""

    async def test_basic_successful_call(self):
        """Standard call returns text and usage."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
        )
        with respx.mock:
            respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response("MiniMax output"))
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_minimax(
                    client, model, "System prompt.", "User prompt."
                )

        assert text == "MiniMax output"
        assert usage == {"input_tokens": 100, "output_tokens": 50}

    async def test_reasoning_split_with_thinking(self):
        """When thinking=True, reasoning_split=True appears in the body."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response())
            )
            async with httpx.AsyncClient() as client:
                await call_minimax(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["reasoning_split"] is True

    async def test_no_reasoning_split_without_thinking(self):
        """When thinking=False, reasoning_split is absent."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
            thinking=False,
        )
        with respx.mock:
            route = respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response())
            )
            async with httpx.AsyncClient() as client:
                await call_minimax(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "reasoning_split" not in parsed

    async def test_system_prompt_in_messages(self):
        """System prompt appears as a system message when non-empty."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
        )
        with respx.mock:
            route = respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response())
            )
            async with httpx.AsyncClient() as client:
                await call_minimax(client, model, "Review carefully.", "Code here.")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["messages"][0] == {"role": "system", "content": "Review carefully."}
        assert parsed["messages"][1] == {"role": "user", "content": "Code here."}

    async def test_empty_system_prompt_omitted(self):
        """Empty system prompt produces only a user message."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
        )
        with respx.mock:
            route = respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response())
            )
            async with httpx.AsyncClient() as client:
                await call_minimax(client, model, "", "Code here.")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert len(parsed["messages"]) == 1
        assert parsed["messages"][0]["role"] == "user"

    async def test_empty_choices_returns_empty_text(self):
        """If the response has no choices, text is empty."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
        )
        response_body = {
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        with respx.mock:
            respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_minimax(client, model, "", "usr")

        assert text == ""


# ===========================================================================
# call_model (dispatcher)
# ===========================================================================


class TestCallModel:
    """Tests for call_model — provider routing dispatcher."""

    async def test_routes_to_anthropic(self):
        """provider='anthropic' dispatches to call_anthropic."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response("from anthropic"))
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "from anthropic"

    async def test_routes_to_minimax(self):
        """provider='minimax' dispatches to call_minimax."""
        model = _make_model(
            provider="minimax",
            model_id="MiniMax-M1",
            api_base="https://api.minimaxi.chat",
        )
        with respx.mock:
            respx.post("https://api.minimaxi.chat/v1/text/chatcompletion_v2").mock(
                return_value=httpx.Response(200, json=_minimax_response("from minimax"))
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "from minimax"

    async def test_routes_to_openai_for_other_providers(self):
        """Any provider not 'anthropic' or 'minimax' goes to call_openai_compatible."""
        for provider_name in ("openai", "google", "together", "custom"):
            model = _make_model(
                provider=provider_name,
                model_id="test-model",
                api_base="https://api.example.com/v1",
            )
            with respx.mock:
                respx.post("https://api.example.com/v1/chat/completions").mock(
                    return_value=httpx.Response(200, json=_openai_response("from openai compat"))
                )
                async with httpx.AsyncClient() as client:
                    text, _ = await call_model(client, model, "sys", "usr")

            assert text == "from openai compat", f"Failed for provider={provider_name}"

    async def test_passes_max_tokens_and_mode(self):
        """max_tokens and mode are forwarded to the underlying provider."""
        model = _make_model(
            provider="anthropic",
            model_id="claude-sonnet-4-20250514",
            thinking=True,
        )
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_model(
                    client, model, "sys", "usr", max_tokens=5000, mode="revision"
                )

        import json
        parsed = json.loads(route.calls.last.request.content)
        budget = _ANTHROPIC_THINKING_BUDGETS["revision"]
        assert parsed["max_tokens"] == 5000 + budget


# ===========================================================================
# call_with_retry
# ===========================================================================


class TestCallWithRetry:
    """Tests for call_with_retry — retry engine with backoff and jitter."""

    async def test_successful_call_no_retry(self):
        """A successful first attempt returns immediately."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response("success"))
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_with_retry(
                    client, model, "sys", "usr", max_retries=3
                )

        assert text == "success"
        assert route.call_count == 1

    async def test_429_retries_with_retry_after(self):
        """HTTP 429 respects Retry-After header and retries."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(
                        429,
                        text="Rate limited",
                        headers={"retry-after": "0.01"},
                    ),
                    httpx.Response(200, json=_anthropic_response("retry ok")),
                ]
            )
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.random.random", return_value=0.0):
                    text, _ = await call_with_retry(
                        client, model, "sys", "usr",
                        max_retries=3,
                        log_fn=log_messages.append,
                    )

        assert text == "retry ok"
        assert any("429" in msg for msg in log_messages)

    async def test_529_aborts_immediately_with_api_error(self):
        """HTTP 529 (overloaded) raises APIError without retrying."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(529, text="Overloaded")
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(APIError, match="overloaded.*529"):
                    await call_with_retry(client, model, "sys", "usr", max_retries=3)

        # Should only be called once (no retries)
        assert route.call_count == 1

    async def test_529_chains_original_exception(self):
        """The APIError from 529 has the original HTTPStatusError as __cause__."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(529, text="Overloaded")
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(APIError) as exc_info:
                    await call_with_retry(client, model, "sys", "usr", max_retries=3)

        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)

    async def test_5xx_retries_with_backoff(self):
        """HTTP 500 errors trigger retries with exponential backoff."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(500, text="Server Error"),
                    httpx.Response(502, text="Bad Gateway"),
                    httpx.Response(200, json=_anthropic_response("recovered")),
                ]
            )
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        text, _ = await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=log_messages.append,
                        )

        assert text == "recovered"
        assert route.call_count == 3
        assert any("500" in msg for msg in log_messages)
        assert any("502" in msg for msg in log_messages)

    async def test_timeout_error_retries(self):
        """httpx.TimeoutException triggers retry."""
        model = _make_model(provider="anthropic")
        call_count = 0

        async def _side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise httpx.ReadTimeout("read timed out")
            return httpx.Response(200, json=_anthropic_response("after timeout"))

        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(side_effect=_side_effect)
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        text, _ = await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=log_messages.append,
                        )

        assert text == "after timeout"
        assert any("ReadTimeout" in msg for msg in log_messages)

    async def test_connect_error_retries(self):
        """httpx.ConnectError triggers retry."""
        model = _make_model(provider="anthropic")
        call_count = 0

        async def _side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json=_anthropic_response("reconnected"))

        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(side_effect=_side_effect)
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        text, _ = await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=log_messages.append,
                        )

        assert text == "reconnected"
        assert any("ConnectError" in msg for msg in log_messages)

    async def test_timeout_hint_logged_only_on_first_attempt(self):
        """The timeout hint is logged only when attempt == 0 and error is TimeoutException."""
        model = _make_model(provider="anthropic", timeout=60)
        call_count = 0

        async def _side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.ReadTimeout("read timed out")
            return httpx.Response(200, json=_anthropic_response("ok"))

        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(side_effect=_side_effect)
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=log_messages.append,
                        )

        hint_messages = [m for m in log_messages if "hint" in m]
        assert len(hint_messages) == 1
        assert "60s" in hint_messages[0]

    async def test_max_retries_exhausted_raises_api_error(self):
        """When all retries fail, APIError is raised with exception chaining."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(500, text="Permanent failure")
            )
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        with pytest.raises(APIError, match="failed after 2 retries") as exc_info:
                            await call_with_retry(
                                client, model, "sys", "usr", max_retries=2
                            )

        # Should have tried 3 times total (initial + 2 retries)
        assert route.call_count == 3
        # Exception chaining
        assert exc_info.value.__cause__ is not None

    async def test_4xx_non_429_non_529_raises_immediately(self):
        """Client errors other than 429 raise APIError without retry."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(APIError, match="HTTP 401"):
                    await call_with_retry(client, model, "sys", "usr", max_retries=3)

        assert route.call_count == 1

    async def test_log_fn_not_required(self):
        """When log_fn is None, no crash on retry logging paths."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(500, text="Error"),
                    httpx.Response(200, json=_anthropic_response("ok")),
                ]
            )
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        text, _ = await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=None,
                        )

        assert text == "ok"

    async def test_jitter_in_backoff(self):
        """Backoff calculation includes random jitter component."""
        model = _make_model(provider="anthropic")
        sleep_values = []

        async def _capture_sleep(seconds):
            sleep_values.append(seconds)

        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(500, text="Error"),
                    httpx.Response(500, text="Error"),
                    httpx.Response(200, json=_anthropic_response("ok")),
                ]
            )
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", side_effect=_capture_sleep):
                    with patch("devils_advocate.providers.random.random", return_value=0.5):
                        await call_with_retry(
                            client, model, "sys", "usr", max_retries=3
                        )

        # attempt 0: 2^0 + 0.5 = 1.5
        # attempt 1: 2^1 + 0.5 = 2.5
        assert len(sleep_values) == 2
        assert sleep_values[0] == pytest.approx(1.5)
        assert sleep_values[1] == pytest.approx(2.5)

    async def test_429_retry_after_takes_precedence(self):
        """When Retry-After > exponential backoff, Retry-After wins."""
        model = _make_model(provider="anthropic")
        sleep_values = []

        async def _capture_sleep(seconds):
            sleep_values.append(seconds)

        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(
                        429, text="Rate limited",
                        headers={"retry-after": "30"},
                    ),
                    httpx.Response(200, json=_anthropic_response("ok")),
                ]
            )
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", side_effect=_capture_sleep):
                    with patch("devils_advocate.providers.random.random", return_value=0.1):
                        await call_with_retry(
                            client, model, "sys", "usr", max_retries=3
                        )

        # attempt 0: max(30, 2^0 + 0.1) = max(30, 1.1) = 30
        assert len(sleep_values) == 1
        assert sleep_values[0] == pytest.approx(30.0)

    async def test_retry_counter_in_log_messages(self):
        """Log messages include the correct retry counter N/max_retries."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            respx.post(ANTHROPIC_API_URL).mock(
                side_effect=[
                    httpx.Response(500, text="Error"),
                    httpx.Response(500, text="Error"),
                    httpx.Response(200, json=_anthropic_response("ok")),
                ]
            )
            log_messages = []
            async with httpx.AsyncClient() as client:
                with patch("devils_advocate.providers.asyncio.sleep", new_callable=AsyncMock):
                    with patch("devils_advocate.providers.random.random", return_value=0.0):
                        await call_with_retry(
                            client, model, "sys", "usr",
                            max_retries=3,
                            log_fn=log_messages.append,
                        )

        assert any("retry 1/3" in msg for msg in log_messages)
        assert any("retry 2/3" in msg for msg in log_messages)

    async def test_default_max_tokens_used(self):
        """When max_tokens is not specified, MAX_OUTPUT_TOKENS is the default."""
        model = _make_model(provider="anthropic")
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(200, json=_anthropic_response())
            )
            async with httpx.AsyncClient() as client:
                await call_with_retry(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["max_tokens"] == MAX_OUTPUT_TOKENS


# ===========================================================================
# call_openai_responses
# ===========================================================================


def _responses_api_response(text="Hello", input_tokens=100, output_tokens=50):
    """Build a mock OpenAI Responses API response body."""
    return {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": text},
                ],
            }
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class TestCallOpenAIResponses:
    """Tests for call_openai_responses — OpenAI Responses API (/v1/responses)."""

    async def test_basic_successful_call(self):
        """Standard call returns text and usage."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response("Codex output"))
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_openai_responses(
                    client, model, "You are a reviewer.", "Review this code."
                )

        assert text == "Codex output"
        assert usage == {"input_tokens": 100, "output_tokens": 50}

    async def test_system_prompt_in_input(self):
        """Non-empty system prompt appears as a system message in 'input'."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "Be precise.", "Question")

        import json
        parsed = json.loads(route.calls.last.request.content)
        messages = parsed["input"]
        assert messages[0] == {"role": "system", "content": "Be precise."}
        assert messages[1] == {"role": "user", "content": "Question"}

    async def test_empty_system_prompt_omitted(self):
        """Empty system prompt produces only a user message in 'input'."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "", "Question")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert len(parsed["input"]) == 1
        assert parsed["input"][0]["role"] == "user"

    async def test_uses_max_output_tokens(self):
        """Body should use 'max_output_tokens' not 'max_tokens'."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "sys", "usr", max_tokens=8192)

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["max_output_tokens"] == 8192
        assert "max_tokens" not in parsed

    async def test_thinking_reasoning_spec_mode(self):
        """With thinking=True and mode=spec, reasoning effort is medium."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "sys", "usr", mode="spec")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["reasoning"] == {"effort": "medium"}

    async def test_thinking_reasoning_plan_mode(self):
        """With thinking=True and mode=plan, reasoning effort is high."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
            thinking=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "sys", "usr", mode="plan")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert parsed["reasoning"] == {"effort": "high"}

    async def test_no_reasoning_when_thinking_disabled(self):
        """When thinking=False, no reasoning key in body."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
            thinking=False,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "sys", "usr")

        import json
        parsed = json.loads(route.calls.last.request.content)
        assert "reasoning" not in parsed

    async def test_multiple_output_blocks_concatenated(self):
        """Multiple output blocks with output_text are concatenated."""
        response_body = {
            "output": [
                {"content": [{"type": "output_text", "text": "Part one. "}]},
                {"content": [{"type": "output_text", "text": "Part two."}]},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, usage = await call_openai_responses(client, model, "sys", "usr")

        assert text == "Part one. Part two."
        assert usage == {"input_tokens": 10, "output_tokens": 20}

    async def test_non_output_text_blocks_ignored(self):
        """Blocks with type != 'output_text' are ignored."""
        response_body = {
            "output": [
                {"content": [{"type": "reasoning", "text": "thinking..."}]},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_openai_responses(client, model, "sys", "usr")

        assert text == ""

    async def test_empty_content_warning_logged(self, caplog):
        """Warns when output_tokens > 0 but visible text is empty."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        response_body = {
            "output": [{"content": [{"type": "reasoning", "text": "..."}]}],
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            with caplog.at_level(logging.WARNING, logger="devils_advocate"):
                async with httpx.AsyncClient() as client:
                    text, usage = await call_openai_responses(
                        client, model, "sys", "usr", max_tokens=4096
                    )

        assert text == ""
        assert usage["output_tokens"] == 200
        assert any("0 visible content" in record.message for record in caplog.records)

    async def test_authorization_header(self):
        """Bearer token is set in the Authorization header."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            route = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response())
            )
            async with httpx.AsyncClient() as client:
                await call_openai_responses(client, model, "sys", "usr")

        headers = route.calls.last.request.headers
        assert headers["authorization"] == "Bearer fake-key-for-testing"

    async def test_http_error_propagates(self):
        """HTTP errors raise httpx.HTTPStatusError."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(500, text="Server Error")
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await call_openai_responses(client, model, "sys", "usr")


# ===========================================================================
# call_model — responses API dispatch
# ===========================================================================


class TestCallModelResponsesAPI:
    """Tests for call_model dispatching to call_openai_responses."""

    async def test_routes_to_responses_api(self):
        """use_responses_api=True dispatches to call_openai_responses."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=_responses_api_response("from responses api"))
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "from responses api"

    async def test_non_responses_api_routes_to_chat_completions(self):
        """use_responses_api=False (default) dispatches to call_openai_compatible."""
        model = _make_model(
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
            use_responses_api=False,
        )
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response("from chat completions"))
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "from chat completions"
