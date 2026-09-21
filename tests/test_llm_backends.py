"""Tests for the LLM backend abstraction (Anthropic + OpenRouter)."""

from __future__ import annotations

import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from devrel_origin.core.llm import LLMClient
from devrel_origin.core.llm_backends import (
    ANTHROPIC_DEFAULT_MODEL,
    AnthropicBackend,
    BackendResponse,
    OpenRouterBackend,
    make_backend,
)

# --- AnthropicBackend ------------------------------------------------------


class TestAnthropicBackend:
    def test_resolve_alias_shorthand(self):
        b = AnthropicBackend(api_key="k")
        assert b.resolve_alias("haiku") == "claude-haiku-4-5-20251001"
        assert b.resolve_alias("sonnet") == ANTHROPIC_DEFAULT_MODEL
        assert b.resolve_alias("opus") == "claude-opus-4-0-20250514"

    def test_resolve_alias_passes_through_unknown(self):
        b = AnthropicBackend(api_key="k")
        assert b.resolve_alias("custom-model-xyz") == "custom-model-xyz"

    @pytest.mark.asyncio
    async def test_chat_returns_normalized_response(self):
        b = AnthropicBackend(api_key="k")
        # Patch the SDK at the messages.create level (existing pattern).
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="hello")]
        mock_response.usage.input_tokens = 12
        mock_response.usage.output_tokens = 7
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_response.model = "claude-sonnet-4-5-20250929"
        b._client.messages.create = AsyncMock(return_value=mock_response)

        out = await b.chat(
            model=ANTHROPIC_DEFAULT_MODEL,
            system_prompt="sys",
            user_prompt="user",
            temperature=0.5,
            max_tokens=100,
        )
        assert isinstance(out, BackendResponse)
        assert out.text == "hello"
        assert out.input_tokens == 12
        assert out.output_tokens == 7
        assert out.model == "claude-sonnet-4-5-20250929"


class TestAnthropicSdkContract:
    """Bind against the REAL installed SDK signature, never a mock of it.

    anthropic 1.0.0 removed `temperature` from `messages.create` (0.125.0 still
    had it). The chat test above replaces `create` with an AsyncMock, which
    accepts any keyword, so every fresh install crashed on its first real call
    while the suite stayed green.
    """

    def test_create_kwargs_bind_to_the_installed_sdk_signature(self):
        b = AnthropicBackend(api_key="k")
        kwargs = b._create_kwargs(
            model="claude-haiku-4-5-20251001",
            system_prompt="sys",
            user_prompt="hi",
            temperature=0.4,
            max_tokens=16,
        )
        # Raises TypeError if the backend sends a keyword this SDK rejects.
        inspect.signature(b._client.messages.create).bind(**kwargs)

    @pytest.mark.parametrize("sdk_accepts_temperature", [True, False])
    def test_temperature_is_sent_only_when_create_accepts_it(self, sdk_accepts_temperature):
        async def create_0x(*, model, max_tokens, messages, system, temperature):
            raise AssertionError("never called")

        async def create_1x(*, model, max_tokens, messages, system):
            raise AssertionError("never called")

        client = MagicMock()
        client.messages.create = create_0x if sdk_accepts_temperature else create_1x
        b = AnthropicBackend(api_key="k", client=client)

        kwargs = b._create_kwargs(
            model="m", system_prompt="s", user_prompt="u", temperature=0.4, max_tokens=8
        )

        assert ("temperature" in kwargs) is sdk_accepts_temperature
        if sdk_accepts_temperature:
            assert kwargs["temperature"] == 0.4
        assert kwargs["messages"] == [{"role": "user", "content": "u"}]
        assert kwargs["system"] == "s"


# --- OpenRouterBackend -----------------------------------------------------


class TestOpenRouterBackend:
    def test_resolve_alias_shorthand_maps_to_anthropic_paths(self):
        b = OpenRouterBackend(api_key="k")
        # OpenRouter uses dot notation (4.5) and rejects Anthropic's dated
        # suffix (-20250929) with a 400 Bad Request.
        assert b.resolve_alias("haiku") == "anthropic/claude-haiku-4.5"
        assert b.resolve_alias("sonnet") == "anthropic/claude-sonnet-4.5"
        assert b.resolve_alias("opus") == "anthropic/claude-opus-4"

    def test_resolve_alias_promotes_bare_anthropic_id(self):
        b = OpenRouterBackend(api_key="k")
        assert (
            b.resolve_alias("claude-sonnet-4-5-20250929") == "anthropic/claude-sonnet-4-5-20250929"
        )

    def test_resolve_alias_passes_through_provider_path(self):
        b = OpenRouterBackend(api_key="k")
        assert b.resolve_alias("openai/gpt-4o-mini") == "openai/gpt-4o-mini"

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_posts_openai_compatible_payload(self):
        respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "gen_xyz",
                    "model": "anthropic/claude-sonnet-4-5-20250929",
                    "choices": [{"message": {"content": "hi from openrouter"}}],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 11},
                },
            )
        )
        b = OpenRouterBackend(api_key="k_or")
        try:
            out = await b.chat(
                model="anthropic/claude-sonnet-4-5-20250929",
                system_prompt="sys",
                user_prompt="user",
                temperature=0.5,
                max_tokens=100,
            )
        finally:
            await b.aclose()
        assert out.text == "hi from openrouter"
        assert out.input_tokens == 42
        assert out.output_tokens == 11
        assert out.model == "anthropic/claude-sonnet-4-5-20250929"

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_extracts_cached_tokens_when_present(self):
        respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "anthropic/claude-sonnet-4-5-20250929",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                },
            )
        )
        b = OpenRouterBackend(api_key="k")
        try:
            out = await b.chat(
                model="anthropic/claude-sonnet-4-5-20250929",
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
                max_tokens=10,
            )
        finally:
            await b.aclose()
        assert out.cache_read_input_tokens == 80


# --- make_backend factory --------------------------------------------------


class TestMakeBackend:
    def test_explicit_anthropic(self):
        with patch.dict(os.environ, {}, clear=False):
            b = make_backend(provider="anthropic", anthropic_api_key="k")
            assert isinstance(b, AnthropicBackend)

    def test_explicit_openrouter(self):
        with patch.dict(os.environ, {}, clear=False):
            b = make_backend(provider="openrouter", openrouter_api_key="k")
            assert isinstance(b, OpenRouterBackend)

    def test_env_only_openrouter_key_picks_openrouter(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k_or"}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            b = make_backend()
            assert isinstance(b, OpenRouterBackend)

    def test_env_with_anthropic_key_picks_anthropic(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "k_a", "OPENROUTER_API_KEY": "k_or"},
            clear=False,
        ):
            b = make_backend()
            assert isinstance(b, AnthropicBackend)

    def test_no_env_defaults_to_anthropic(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            b = make_backend()
            assert isinstance(b, AnthropicBackend)


# --- LLMClient with explicit backend + per-agent overrides -----------------


class TestLLMClientBackendIntegration:
    @pytest.mark.asyncio
    async def test_client_uses_explicit_backend(self):
        backend = MagicMock()
        backend.name = "openrouter"
        backend.default_model = "anthropic/claude-sonnet-4-5-20250929"
        backend.cheap_model = "anthropic/claude-haiku-4-5-20251001"
        backend.resolve_alias = lambda alias: alias
        backend.chat = AsyncMock(
            return_value=BackendResponse(
                text="from-backend",
                model="anthropic/claude-sonnet-4-5-20250929",
                input_tokens=10,
                output_tokens=5,
            )
        )

        client = LLMClient(backend=backend)
        out = await client.generate(system_prompt="s", user_prompt="u")
        assert out == "from-backend"
        assert backend.chat.await_args.kwargs["model"] == ("anthropic/claude-sonnet-4-5-20250929")

    @pytest.mark.asyncio
    async def test_per_agent_model_override_routes_correctly(self):
        backend = MagicMock()
        backend.name = "openrouter"
        backend.default_model = "anthropic/claude-sonnet-4-5-20250929"
        backend.cheap_model = "anthropic/claude-haiku-4-5-20251001"
        backend.resolve_alias = lambda alias: alias
        backend.chat = AsyncMock(
            return_value=BackendResponse(text="ok", model="x", input_tokens=1, output_tokens=1)
        )

        client = LLMClient(
            backend=backend,
            agent_models={
                "argus": "openai/gpt-4o-mini",
                "kai": "anthropic/claude-opus-4-0-20250514",
            },
        )

        with client.agent_context("argus"):
            await client.generate(system_prompt="s", user_prompt="u")
        assert backend.chat.await_args.kwargs["model"] == "openai/gpt-4o-mini"

        with client.agent_context("kai"):
            await client.generate(system_prompt="s", user_prompt="u")
        assert backend.chat.await_args.kwargs["model"] == ("anthropic/claude-opus-4-0-20250514")

    @pytest.mark.asyncio
    async def test_explicit_model_arg_wins_over_per_agent_override(self):
        backend = MagicMock()
        backend.name = "openrouter"
        backend.default_model = "x"
        backend.cheap_model = "y"
        backend.resolve_alias = lambda a: a
        backend.chat = AsyncMock(
            return_value=BackendResponse(text="ok", model="x", input_tokens=1, output_tokens=1)
        )

        client = LLMClient(
            backend=backend,
            agent_models={"argus": "openai/gpt-4o-mini"},
        )
        with client.agent_context("argus"):
            await client.generate(
                system_prompt="s", user_prompt="u", model="anthropic/claude-opus-4-0-20250514"
            )
        assert backend.chat.await_args.kwargs["model"] == "anthropic/claude-opus-4-0-20250514"

    @pytest.mark.asyncio
    async def test_budget_downgrade_uses_backend_cheap_model(self):
        backend = MagicMock()
        backend.name = "openrouter"
        backend.default_model = "anthropic/claude-sonnet-4-5-20250929"
        backend.cheap_model = "anthropic/claude-haiku-4-5-20251001"
        backend.resolve_alias = lambda a: a
        backend.chat = AsyncMock(
            return_value=BackendResponse(text="ok", model="x", input_tokens=1, output_tokens=1)
        )

        client = LLMClient(backend=backend)
        client._budget_exhausted = True
        await client.generate(system_prompt="s", user_prompt="u")
        assert backend.chat.await_args.kwargs["model"] == "anthropic/claude-haiku-4-5-20251001"

    def test_back_compat_client_property_exposes_anthropic_sdk(self):
        client = LLMClient(api_key="k")
        # AnthropicBackend default exposes _client; OpenRouter would return None.
        assert client._client is not None

    def test_client_picks_openrouter_when_env_set(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            client = LLMClient()
            assert client.backend.name == "openrouter"
