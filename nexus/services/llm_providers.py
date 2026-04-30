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
)


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
    if "api.anthropic.com" in base:
        return "anthropic"
    if "openrouter.ai" in base:
        return "openrouter"
    if "api.groq.com" in base:
        return "groq"
    if "api.openai.com" in base:
        return "openai"
    return "openai_compatible"


def canonical_base_url_v1(provider: str, base_url_v1: str | None = None) -> str:
    base = (base_url_v1 or "").strip().rstrip("/")
    p = normalize_provider(provider, base)
    if base:
        if p in ("openai", "openai_compatible", "openrouter", "groq") and base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")].rstrip("/")
        if p == "anthropic":
            if base.endswith("/v1/messages"):
                base = base[: -len("/v1/messages")].rstrip("/")
            elif base.endswith("/v1/models"):
                base = base[: -len("/v1/models")].rstrip("/")
        return base
    defaults = {
        "openai": "https://api.openai.com/v1",
        "openai_compatible": "",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "anthropic": "https://api.anthropic.com",
    }
    return defaults.get(p, "")


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
    return headers


def provider_models_url(provider: str, base_url_v1: str) -> str:
    p = normalize_provider(provider, base_url_v1)
    base = canonical_base_url_v1(p, base_url_v1)
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
    elif p == "anthropic":
        # Defensive fallback for variant payloads.
        items = payload.get("models") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    out.append(str(item["id"]))
    seen = set()
    uniq = []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def list_provider_models(*, provider: str, base_url_v1: str, api_key: str, timeout_s: int = 12) -> List[str]:
    p = normalize_provider(provider, base_url_v1)
    url = provider_models_url(p, base_url_v1)
    if not url:
        return []
    r = requests.get(url, headers=provider_headers(p, api_key, json_body=False), timeout=timeout_s)
    r.raise_for_status()
    return _extract_models(p, r.json() or {})


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
        payload = {
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
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        }

    r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json() or {}
    if p == "anthropic":
        content = data.get("content") or []
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
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
            system_prompt="Você é um health check. Responda apenas OK.",
            user_prompt="OK?",
            timeout_s=timeout_s,
        )
        out["ok"] = bool(str(sample).strip())
        out["sample"] = str(sample)[:120]
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out
