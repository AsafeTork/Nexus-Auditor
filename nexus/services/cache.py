from __future__ import annotations

import json
import os
from typing import Any, Optional

import redis


_REDIS: redis.Redis | None = None


def _redis_conn() -> redis.Redis:
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # Keep timeouts small in low-resource environments.
    _REDIS = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
    return _REDIS


def cache_get_json(key: str) -> Optional[dict]:
    try:
        raw = _redis_conn().get(key)
        if not raw:
            return None
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except Exception:
        return None


def cache_set_json(key: str, value: dict, ttl_s: int) -> None:
    try:
        _redis_conn().set(key, json.dumps(value, ensure_ascii=False), ex=int(ttl_s))
    except Exception:
        return


def cache_get_text(key: str) -> Optional[str]:
    try:
        raw = _redis_conn().get(key)
        return raw if raw else None
    except Exception:
        return None


def cache_set_text(key: str, value: str, ttl_s: int) -> None:
    try:
        _redis_conn().set(key, value, ex=int(ttl_s))
    except Exception:
        return

