"""Configurable AI provider layer.

Every pipeline step asks for JSON that validates against a schema. Providers:
  anthropic          - Anthropic SDK (default; structured outputs + adaptive thinking)
  openai             - OpenAI SDK (chat completions, json_schema response format)
  openai_compatible  - any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio...)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import get_secret, load_config


class ProviderError(RuntimeError):
    pass


class SpendLimitError(ProviderError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    raw_text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    model: str = ""
    provider: str = ""
    stop_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# USD per 1M tokens: (input, output, cache_read, cache_write_5m)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "gpt-5": (1.25, 10.0, 0.125, 0.0),
    "gpt-5-mini": (0.25, 2.0, 0.025, 0.0),
    "gpt-4.1": (2.0, 8.0, 0.5, 0.0),
    "gpt-4o": (2.5, 10.0, 1.25, 0.0),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    price = None
    for k, v in PRICES.items():
        if model.startswith(k):
            price = v
            break
    if price is None:
        return 0.0
    pin, pout, pcr, pcw = price
    return (tokens_in * pin + tokens_out * pout + cache_read * pcr + cache_write * pcw) / 1_000_000


class Provider:
    name = "base"

    def __init__(self, model: str, **kwargs: Any):
        self.model = model
        self.kwargs = kwargs

    def generate_json(self, system: str, user: str | list[dict[str, Any]], schema: dict[str, Any],
                      max_tokens: int = 32000, step: str = "") -> LLMResult:
        raise NotImplementedError

    def check(self) -> dict[str, Any]:
        """Cheap connectivity/credential check."""
        raise NotImplementedError


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, effort: str = "medium", **kwargs: Any):
        super().__init__(model, **kwargs)
        import anthropic

        key = get_secret("ANTHROPIC_API_KEY")
        # anthropic.Anthropic() also resolves `ant auth login` profiles when no key is set.
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.effort = effort
        self._anthropic = anthropic

    def _supports_fallbacks(self) -> bool:
        return self.model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5"))

    def generate_json(self, system, user, schema, max_tokens=32000, step=""):
        anthropic = self._anthropic
        messages = [{"role": "user", "content": user}]
        t0 = time.time()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if not self.model.startswith(("claude-haiku", "claude-3", "claude-sonnet-4-5", "claude-opus-4-5")):
            kwargs["thinking"] = {"type": "adaptive"}
        try:
            if self._supports_fallbacks():
                with self.client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                ) as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic authentication failed - check ANTHROPIC_API_KEY in Settings.") from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Anthropic rejected the request: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Anthropic rate limit hit; the job will retry.") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Could not reach the Anthropic API (network).") from e

        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise ProviderError(f"The model declined this request (category: {cat}).")
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            raise ProviderError(f"Output truncated at {max_tokens} tokens; step '{step}' needs a smaller chunk.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"Model returned invalid JSON in step '{step}': {e}") from e
        u = msg.usage
        cr = u.cache_read_input_tokens or 0
        cw = u.cache_creation_input_tokens or 0
        res = LLMResult(
            data=data, raw_text=text, tokens_in=u.input_tokens, tokens_out=u.output_tokens,
            cache_read=cr, cache_write=cw, seconds=time.time() - t0, model=msg.model,
            provider=self.name, stop_reason=msg.stop_reason or "",
        )
        res.cost_usd = estimate_cost(msg.model, u.input_tokens, u.output_tokens, cr, cw)
        return res

    def check(self):
        try:
            m = self.client.models.retrieve(self.model)
            return {"ok": True, "detail": f"Connected. Model {m.id} available."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, base_url: str | None = None, compatible: bool = False, **kwargs: Any):
        super().__init__(model, **kwargs)
        from openai import OpenAI

        if compatible:
            self.name = "openai_compatible"
            key = get_secret("OPENAI_COMPATIBLE_API_KEY") or "not-needed"
            self.client = OpenAI(api_key=key, base_url=base_url or None)
        else:
            key = get_secret("OPENAI_API_KEY")
            if not key:
                raise ProviderError("OPENAI_API_KEY is not configured - add it in Settings.")
            self.client = OpenAI(api_key=key)
        self.compatible = compatible

    def generate_json(self, system, user, schema, max_tokens=32000, step=""):
        import openai

        if isinstance(user, list):
            # convert Anthropic-style content blocks to OpenAI content parts
            parts = []
            for blk in user:
                if blk.get("type") == "text":
                    parts.append({"type": "text", "text": blk["text"]})
                elif blk.get("type") == "image":
                    src = blk["source"]
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{src['media_type']};base64,{src['data']}"}})
            content: Any = parts
        else:
            content = user
        t0 = time.time()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": not self.compatible}},
        )
        if self.model.startswith(("gpt-5", "o1", "o3", "o4")):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = min(max_tokens, 16384)
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise ProviderError("OpenAI authentication failed - check the API key in Settings.") from e
        except openai.BadRequestError as e:
            # Some compatible servers reject json_schema; retry with json_object
            if "response_format" in str(e) or "json_schema" in str(e):
                kwargs["response_format"] = {"type": "json_object"}
                kwargs["messages"][0]["content"] += "\n\nRespond ONLY with JSON matching this schema:\n" + json.dumps(schema)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise ProviderError(f"Provider rejected the request: {e}") from e
        except openai.RateLimitError as e:
            raise ProviderError("Rate limit hit; the job will retry.") from e
        except openai.APIConnectionError as e:
            raise ProviderError(f"Could not reach provider at {self.client.base_url}.") from e
        except openai.APIStatusError as e:
            raise ProviderError(f"Provider API error {e.status_code}: {e}") from e
        choice = resp.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length":
            raise ProviderError(f"Output truncated; step '{step}' needs a smaller chunk.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"Model returned invalid JSON in step '{step}': {e}") from e
        u = resp.usage
        tin = u.prompt_tokens if u else 0
        tout = u.completion_tokens if u else 0
        cached = 0
        if u and getattr(u, "prompt_tokens_details", None):
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        res = LLMResult(data=data, raw_text=text, tokens_in=tin - cached, tokens_out=tout, cache_read=cached,
                        seconds=time.time() - t0, model=resp.model or self.model, provider=self.name,
                        stop_reason=choice.finish_reason or "")
        res.cost_usd = estimate_cost(res.model, res.tokens_in, tout, cached)
        return res

    def check(self):
        try:
            self.client.models.retrieve(self.model)
            return {"ok": True, "detail": f"Connected. Model {self.model} available."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def get_provider(cfg: dict[str, Any] | None = None) -> Provider:
    cfg = cfg or load_config()
    name = cfg.get("provider", "anthropic")
    model = cfg.get("model", "claude-opus-5-5")
    if name == "mock":
        from .mock import MockProvider
        return MockProvider()
    if name == "anthropic":
        if not get_secret("ANTHROPIC_API_KEY"):
            # allow `ant auth login` profiles; the SDK resolves them itself
            import os
            if not os.path.exists(os.path.expanduser("~/.config/anthropic")):
                raise ProviderError("No Anthropic credentials. Add ANTHROPIC_API_KEY in Settings "
                                    "(or switch provider to OpenAI).")
        return AnthropicProvider(model, effort=cfg.get("effort", "medium"))
    if name == "openai":
        return OpenAIProvider(model)
    if name == "openai_compatible":
        return OpenAIProvider(model, base_url=cfg.get("openai_base_url") or None, compatible=True)
    raise ProviderError(f"Unknown provider '{name}'")
