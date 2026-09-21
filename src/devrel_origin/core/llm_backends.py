"""LLM provider backends for multi-provider support.

LLMClient delegates the actual chat call to a backend so different providers
(Anthropic direct, OpenRouter, future ones) can be swapped without rewriting
the cost-tracking, budget-gating, agent-attribution layers in core/llm.py.

A backend's responsibility is narrow: take a system prompt + user prompt +
generation params + a model id, return the response text plus token usage in
a normalized shape. Caching, retry, and rate limiting can be done inside the
backend if the provider supports it; the client layer doesn't care.

Each backend exposes:
- `name`: short id used in logs and config (`"anthropic"`, `"openrouter"`)
- `default_model`: backend-default when the caller doesn't override
- `cheap_model`: budget-downgrade target (used by BudgetGate)
- `resolve_alias(alias)`: translates `"haiku"`/`"sonnet"`/`"opus"` shorthand
  to a real model id (backend-specific; OpenRouter uses dot notation without
  date suffix like `anthropic/claude-haiku-4.5`, native Anthropic wants the
  bare dated id like `claude-haiku-4-5-20251001`)
- async `chat(...)`: the actual call, returns a `BackendResponse`
- async `aclose()`: release any underlying clients (httpx pools etc.)
"""

from __future__ import annotations

import inspect
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendResponse:
    """Normalized response shape from any LLM backend."""

    text: str
    model: str  # the model id that actually responded (provider may downgrade)
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    raw_meta: dict[str, Any] = field(default_factory=dict)


class LLMBackend(ABC):
    """Abstract LLM backend. Every concrete impl handles one provider."""

    name: str = "abstract"
    default_model: str = ""
    cheap_model: str = ""

    @abstractmethod
    def resolve_alias(self, alias: str) -> str:
        """Map shorthand ('haiku' / 'sonnet' / 'opus' / explicit id) to the
        backend's model identifier. Pass-through for ids the backend already
        recognizes; the client layer hands them to the backend as-is."""

    @abstractmethod
    async def chat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> BackendResponse:
        """Send a chat completion and return the normalized response."""

    async def aclose(self) -> None:  # noqa: B027 - intentional opt-in hook
        """Optional teardown for backends with persistent clients."""


# --- Anthropic --------------------------------------------------------------

# Native Anthropic model ids. OpenRouter exposes the same Claude models under
# `anthropic/<id>` paths.
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
ANTHROPIC_MODELS: dict[str, str] = {
    "opus": "claude-opus-4-0-20250514",
    "sonnet": ANTHROPIC_DEFAULT_MODEL,
    "haiku": "claude-haiku-4-5-20251001",
}


def _sdk_accepts_temperature(create: Any) -> bool:
    """anthropic 1.0.0 removed `temperature` from `messages.create`; 0.125.0
    still had it. Probe the installed SDK instead of pinning a version range,
    so both generations work and a future SDK needs no code change here."""
    try:
        return "temperature" in inspect.signature(create).parameters
    except (TypeError, ValueError):
        return True  # cannot introspect: keep the historical behaviour


class AnthropicBackend(LLMBackend):
    """Direct Anthropic API via the official SDK. Default backend."""

    name = "anthropic"
    default_model = ANTHROPIC_DEFAULT_MODEL
    cheap_model = ANTHROPIC_MODELS["haiku"]

    def __init__(self, api_key: str = "", client: AsyncAnthropic | None = None):
        # Empty key: pass through 'dummy' so the SDK constructs (used by tests
        # that mock out messages.create); a real call would still 401.
        self._client = client or AsyncAnthropic(api_key=api_key or "dummy")
        self._send_temperature = _sdk_accepts_temperature(self._client.messages.create)

    def resolve_alias(self, alias: str) -> str:
        return ANTHROPIC_MODELS.get(alias, alias)

    def _create_kwargs(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Arguments for `messages.create`, limited to what this SDK accepts.
        On anthropic >= 1.0 the requested temperature is dropped, because the
        SDK no longer exposes it and passing it raises TypeError."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self._send_temperature:
            kwargs["temperature"] = temperature
        return kwargs

    async def chat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> BackendResponse:
        response = await self._client.messages.create(
            **self._create_kwargs(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
        return BackendResponse(
            text=response.content[0].text,
            model=response.model if hasattr(response, "model") else model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
            or 0,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

    async def aclose(self) -> None:
        # AsyncAnthropic owns an internal httpx client; close it via SDK.
        try:
            await self._client.close()
        except Exception:
            pass


# --- OpenRouter -------------------------------------------------------------

# OpenRouter is OpenAI-compatible. We POST to /chat/completions with model ids
# in the form `<provider>/<model>` (e.g. `anthropic/claude-sonnet-4.5`,
# `openai/gpt-4o-mini`). OpenRouter uses dot notation for Anthropic versions
# and does NOT accept Anthropic's dated suffixes (`-20250929`); a 400 Bad
# Request is the symptom of using the dated id here. Pricing is per-model;
# response usage is OpenAI-shape (prompt_tokens / completion_tokens).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
OPENROUTER_CHEAP_MODEL = "anthropic/claude-haiku-4.5"
OPENROUTER_ALIASES: dict[str, str] = {
    "opus": "anthropic/claude-opus-4",
    "sonnet": OPENROUTER_DEFAULT_MODEL,
    "haiku": OPENROUTER_CHEAP_MODEL,
}


class OpenRouterBackend(LLMBackend):
    """OpenRouter via OpenAI-compatible HTTP endpoint.

    No additional SDK dependency; we use the existing httpx core dep. Set
    OPENROUTER_API_KEY in the environment, or pass api_key explicitly.
    Optionally set OPENROUTER_REFERER + OPENROUTER_TITLE for OpenRouter's
    leaderboard attribution.
    """

    name = "openrouter"
    default_model = OPENROUTER_DEFAULT_MODEL
    cheap_model = OPENROUTER_CHEAP_MODEL

    def __init__(
        self,
        api_key: str = "",
        *,
        referer: str | None = None,
        title: str | None = None,
        timeout: float = 120.0,
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._referer = referer or os.environ.get(
            "OPENROUTER_REFERER", "https://github.com/dovzhikova/devrel-origin"
        )
        self._title = title or os.environ.get("OPENROUTER_TITLE", "devrel-origin")
        self._client = httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "HTTP-Referer": self._referer,
                "X-Title": self._title,
                "Content-Type": "application/json",
            },
        )

    def resolve_alias(self, alias: str) -> str:
        # Allow the caller to pass either a shorthand (haiku/sonnet/opus), a
        # plain Anthropic id (claude-sonnet-4-5-...), or an already-prefixed
        # OpenRouter path (anthropic/claude-sonnet-4-5-..., openai/gpt-4o).
        if alias in OPENROUTER_ALIASES:
            return OPENROUTER_ALIASES[alias]
        if alias in ANTHROPIC_MODELS.values() or alias.startswith("claude-"):
            return f"anthropic/{alias}"
        return alias  # already provider-qualified or unknown

    async def chat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> BackendResponse:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        return BackendResponse(
            text=text,
            model=data.get("model") or model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            # OpenAI-compat usage doesn't carry Anthropic-style cache creation /
            # read tokens. Some upstream providers route them via
            # `prompt_tokens_details.cached_tokens`; surface them when present.
            cache_read_input_tokens=int(
                ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
            ),
            raw_meta={"id": data.get("id"), "provider": data.get("provider")},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# --- Factory ----------------------------------------------------------------


def make_backend(
    provider: str | None = None,
    *,
    anthropic_api_key: str = "",
    openrouter_api_key: str = "",
) -> LLMBackend:
    """Construct a backend by name, falling back to env-var auto-detect.

    Resolution order:
      1. Explicit `provider` arg ('anthropic' | 'openrouter')
      2. OPENROUTER_API_KEY set and ANTHROPIC_API_KEY unset -> openrouter
      3. Default -> anthropic (preserves pre-multi-provider behavior)
    """
    if provider == "openrouter":
        return OpenRouterBackend(api_key=openrouter_api_key)
    if provider == "anthropic":
        return AnthropicBackend(api_key=anthropic_api_key)

    has_or = bool(openrouter_api_key or os.environ.get("OPENROUTER_API_KEY"))
    has_ant = bool(anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if has_or and not has_ant:
        return OpenRouterBackend(api_key=openrouter_api_key)
    return AnthropicBackend(api_key=anthropic_api_key)
