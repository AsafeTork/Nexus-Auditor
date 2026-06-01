from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

import requests


SUPPORTED_LLM_PROVIDERS = (
    "openai",
    "openai_compatible",
    "openrouter",
    "groq",
    "anthropic",
    "deepseek",
    "together",
    "mistral",
    "fireworks",
    "perplexity",
    "gemini",
    "eclipse",
)

# Canonical base URLs for each named provider.
_PROVIDER_DEFAULTS: Dict[str, str] = {
    "openai":           "https://api.openai.com/v1",
    "openai_compatible": "",
    "openrouter":       "https://openrouter.ai/api/v1",
    "groq":             "https://api.groq.com/openai/v1",
    "anthropic":        "https://api.anthropic.com",
    "deepseek":         "https://api.deepseek.com",
    "together":         "https://api.together.ai/v1",
    "mistral":          "https://api.mistral.ai/v1",
    "fireworks":        "https://api.fireworks.ai/inference/v1",
    "perplexity":       "https://api.perplexity.ai",
    "gemini":           "https://generativelanguage.googleapis.com/v1beta/openai",
    "eclipse":          "https://eclipseprovider.pro/v1",
}

# Fallback model lists for providers whose /v1/models endpoint is not public.
_PROVIDER_FALLBACK_MODELS: Dict[str, List[str]] = {
    "perplexity": [
        "sonar",
        "sonar-pro",
        "sonar-reasoning",
        "sonar-reasoning-pro",
        "sonar-deep-research",
    ],
    "fireworks": [
        "accounts/fireworks/models/deepseek-v3p1",
        "accounts/fireworks/models/deepseek-v3p2",
        "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "accounts/fireworks/models/qwen2p5-72b-instruct",
        "accounts/fireworks/models/kimi-k2-instruct-0905",
    ],
}

# Hostname fragments used to auto-detect provider from base_url.
_URL_PROVIDER_MAP: List[tuple[str, str]] = [
    ("api.anthropic.com",                        "anthropic"),
    ("openrouter.ai",                            "openrouter"),
    ("api.groq.com",                             "groq"),
    ("api.openai.com",                           "openai"),
    ("api.deepseek.com",                         "deepseek"),
    ("api.together.ai",                          "together"),
    ("api.together.xyz",                         "together"),
    ("api.mistral.ai",                           "mistral"),
    ("api.fireworks.ai",                         "fireworks"),
    ("api.perplexity.ai",                        "perplexity"),
    ("generativelanguage.googleapis.com",        "gemini"),
    ("eclipseprovider.pro",                      "eclipse"),
    ("eclipse.mestredoblack.pro",                "eclipse"),  # legacy alias
]


@dataclass
class ProviderConfig:
    provider: str
    base_url_v1: str
    api_key: str
    model: str = ""


def normalize_provider(provider: str | None, base_url_v1: str | None = None) -> str:
    p = (provider or "").strip().lower()
    if p in SUPPORTED_LLM_PROVIDERS:
        return p
    base = (base_url_v1 or "").strip().lower()
    for fragment, name in _URL_PROVIDER_MAP:
        if fragment in base:
            return name
    return "openai_compatible"


def canonical_base_url_v1(provider: str, base_url_v1: str | None = None) -> str:
    base = (base_url_v1 or "").strip().rstrip("/")
    p = normalize_provider(provider, base)

    if base:
        # Strip trailing endpoint paths so callers can pass a full completion URL.
        for suffix in ("/chat/completions", "/completions", "/messages"):
            if base.endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        # For Anthropic the base URL has no /v1 path component.
        if p == "anthropic":
            for suffix in ("/v1/messages", "/v1/models", "/v1"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)].rstrip("/")
                    break
        return base

    return _PROVIDER_DEFAULTS.get(p, "")


def provider_headers(provider: str, api_key: str, *, json_body: bool = True) -> Dict[str, str]:
    p = normalize_provider(provider)
    headers: Dict[str, str] = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if api_key:
        if p == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    # OpenRouter asks for optional attribution headers to appear in rankings.
    if p == "openrouter":
        headers["HTTP-Referer"] = "https://xentinel.ai"
        headers["X-Title"] = "Xentinel"
    return headers


def provider_models_url(provider: str, base_url_v1: str) -> str:
    p = normalize_provider(provider, base_url_v1)
    base = canonical_base_url_v1(p, base_url_v1)
    if not base:
        return ""
    if p == "anthropic":
        return base.rstrip("/") + "/v1/models"
    return base.rstrip("/") + "/models"


def provider_chat_url(provider: str, base_url_v1: str) -> str:
    p = normalize_provider(provider, base_url_v1)
    base = canonical_base_url_v1(p, base_url_v1)
    if p == "anthropic":
        return base.rstrip("/") + "/v1/messages"
    return base.rstrip("/") + "/chat/completions"


def _extract_models(provider: str, payload: Any) -> List[str]:
    p = normalize_provider(provider)
    out: List[str] = []
    if not isinstance(payload, dict):
        return out
    data = payload.get("data") or payload.get("models") or []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("name")
            if mid:
                out.append(str(mid))
    seen: set = set()
    uniq = []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def list_provider_models(*, provider: str, base_url_v1: str, api_key: str, timeout_s: int = 12) -> List[str]:
    p = normalize_provider(provider, base_url_v1)

    # Providers with no public /v1/models endpoint — return a curated fallback list.
    if p in _PROVIDER_FALLBACK_MODELS and not api_key:
        return _PROVIDER_FALLBACK_MODELS[p]

    url = provider_models_url(p, base_url_v1)
    if not url:
        return _PROVIDER_FALLBACK_MODELS.get(p, [])

    try:
        r = requests.get(url, headers=provider_headers(p, api_key, json_body=False), timeout=timeout_s)
        r.raise_for_status()
        models = _extract_models(p, r.json() or {})
        if models:
            return models
    except Exception:
        pass

    # Fall back to known model list if the endpoint fails or returns nothing.
    return _PROVIDER_FALLBACK_MODELS.get(p, [])


def call_provider_non_stream(
    *,
    provider: str,
    base_url_v1: str,
    api_key: str,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int = 120,
) -> str:
    p = normalize_provider(provider, base_url_v1)
    url = provider_chat_url(p, base_url_v1)
    headers = provider_headers(p, api_key, json_body=True)

    if p == "anthropic":
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 4000,
            "temperature": float(temperature),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    else:
        payload = {
            "model": model,
            "temperature": float(temperature),
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

    r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json() or {}

    if p == "anthropic":
        content = data.get("content") or []
        if isinstance(content, list):
            parts = [str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
            return "".join(parts).strip()
        return ""

    return str(((data.get("choices") or [None])[0] or {}).get("message", {}).get("content") or "")


def validate_provider(
    *,
    provider: str,
    base_url_v1: str,
    api_key: str,
    model: str = "",
    timeout_s: int = 15,
) -> Dict[str, Any]:
    p = normalize_provider(provider, base_url_v1)
    base = canonical_base_url_v1(p, base_url_v1)
    out: Dict[str, Any] = {"provider": p, "base_url_v1": base, "model": model}
    models = list_provider_models(provider=p, base_url_v1=base, api_key=api_key, timeout_s=timeout_s)
    out["models_count"] = len(models)
    out["models_sample"] = models[:10]
    if model:
        out["model_found"] = bool(model in models)
    try:
        sample = call_provider_non_stream(
            provider=p,
            base_url_v1=base,
            api_key=api_key,
            model=model or (models[0] if models else ""),
            temperature=0.0,
            system_prompt="You are a health check. Reply only with OK.",
            user_prompt="OK?",
            timeout_s=timeout_s,
        )
        out["ok"] = bool(str(sample).strip())
        out["sample"] = str(sample)[:120]
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out
