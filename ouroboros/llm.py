"""
Ouroboros — LLM client.

Multi-provider routing: OpenAI (primary), Google Gemini, Anthropic (via OpenRouter).
All providers use the openai Python SDK with different base_url and api_key.
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "gemini-2.5-flash"


def _detect_provider(model: str) -> Tuple[str, str, str]:
    """Detect provider from model name. Returns (base_url, api_key, clean_model_name).

    Routing rules:
      - "openai/" prefix or known OpenAI models (gpt-*, o3*, o4*) -> OpenAI direct
      - "google/" prefix or "gemini*" -> Google Gemini (OpenAI-compatible endpoint)
      - "anthropic/" prefix -> OpenRouter (Anthropic has no OpenAI-compatible API)
      - No prefix -> default to OpenAI
    """
    if model.startswith("openai/"):
        return (
            "https://api.openai.com/v1",
            os.environ.get("OPENAI_API_KEY", ""),
            model[7:],  # strip "openai/" prefix for direct API
        )
    elif model.startswith("google/") or model.startswith("gemini"):
        clean = model.replace("google/", "")
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            os.environ.get("GOOGLE_API_KEY", ""),
            clean,
        )
    elif model.startswith("anthropic/"):
        # Anthropic models go through OpenRouter (no native OpenAI-compatible API)
        return (
            "https://openrouter.ai/api/v1",
            os.environ.get("OPENROUTER_API_KEY", ""),
            model,  # keep full name for OpenRouter
        )
    elif model.startswith(("x-ai/", "meta-llama/", "qwen/")):
        # Other providers route through OpenRouter
        return (
            "https://openrouter.ai/api/v1",
            os.environ.get("OPENROUTER_API_KEY", ""),
            model,
        )
    else:
        # Default: treat as OpenAI model (gpt-4.1, o3, etc.)
        return (
            "https://api.openai.com/v1",
            os.environ.get("OPENAI_API_KEY", ""),
            model,
        )


def _is_openrouter(base_url: str) -> bool:
    """Check if a base_url points to OpenRouter."""
    return "openrouter.ai" in base_url


def _is_anthropic_model(model: str) -> bool:
    """Check if a model is an Anthropic model (routed via OpenRouter)."""
    return model.startswith("anthropic/")


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.
    Only runs if OPENROUTER_API_KEY is set.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure or if key is not configured.
    """
    if not os.environ.get("OPENROUTER_API_KEY", ""):
        log.debug("OPENROUTER_API_KEY not set, skipping pricing fetch")
        return {}

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except Exception as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """Multi-provider LLM client. Routes to OpenAI, Gemini, or OpenRouter based on model name."""

    def __init__(self, **kwargs):
        # Accept (and ignore) legacy kwargs for backward compatibility
        # Old code passed api_key= and base_url= which are no longer needed
        # since provider detection is automatic.
        # Per-provider client cache: base_url -> OpenAI client
        self._clients: Dict[str, Any] = {}

    def _get_client(self, base_url: str, api_key: str):
        """Get or create an OpenAI client for the given provider."""
        cache_key = base_url
        if cache_key not in self._clients:
            from openai import OpenAI
            headers = {"X-Title": "Ouroboros"}
            if _is_openrouter(base_url):
                headers["HTTP-Referer"] = "https://github.com/razzant/ouroboros"
            self._clients[cache_key] = OpenAI(
                base_url=base_url,
                api_key=api_key,
                default_headers=headers,
            )
        return self._clients[cache_key]

    def _fetch_generation_cost(self, generation_id: str, base_url: str, api_key: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API. Only works for OpenRouter."""
        if not _is_openrouter(base_url):
            return None
        try:
            import requests
            url = f"{base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet -- retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Routes to correct provider based on model name.
        Returns: (response_message_dict, usage_dict with cost)."""
        base_url, api_key, clean_model = _detect_provider(model)
        client = self._get_client(base_url, api_key)
        effort = normalize_reasoning_effort(reasoning_effort)
        is_openrouter = _is_openrouter(base_url)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        # OpenRouter-specific: pin Anthropic models to Anthropic provider for prompt caching
        if is_openrouter and _is_anthropic_model(model):
            extra_body["provider"] = {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": clean_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if tools:
            if is_openrouter and _is_anthropic_model(model):
                # Add cache_control to last tool for Anthropic prompt caching (via OpenRouter)
                tools_with_cache = [t for t in tools]  # shallow copy
                if tools_with_cache:
                    last_tool = {**tools_with_cache[-1]}  # copy last tool
                    last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                    tools_with_cache[-1] = last_tool
                kwargs["tools"] = tools_with_cache
            else:
                kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens from prompt_tokens_details if available
        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        # Fetch cost from OpenRouter Generation API if not in usage (OpenRouter-only)
        if not usage.get("cost") and is_openrouter:
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id, base_url, api_key)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "gpt-4.1",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight -- no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} -- for URL images
                - {"base64": "<b64>", "mime": "image/png"} -- for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return os.environ.get("OUROBOROS_MODEL", "gpt-4.1")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "gpt-4.1")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
