from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests


_CATCHALL_BASE = "https://catchall.newscatcherapi.com"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _headers(api_key: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "x-api-key": api_key}


def catchall_submit_job(
    *,
    api_key: str,
    query: str,
    context: str,
    start_date: str,
    end_date: str,
    limit: int = 10,
    mode: str = "lite",
) -> str:
    url = _CATCHALL_BASE + "/catchAll/submit"
    payload = {
        "query": query,
        "context": context,
        "start_date": start_date,
        "end_date": end_date,
        "limit": int(limit),
        "mode": mode,
    }
    r = requests.post(url, headers=_headers(api_key), json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    job_id = str(j.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("catchall_submit_job: missing job_id")
    return job_id


def catchall_status(*, api_key: str, job_id: str) -> Dict[str, Any]:
    url = _CATCHALL_BASE + f"/catchAll/status/{job_id}"
    r = requests.get(url, headers={"x-api-key": api_key}, timeout=20)
    r.raise_for_status()
    return r.json()


def catchall_pull(*, api_key: str, job_id: str, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    url = _CATCHALL_BASE + f"/catchAll/pull/{job_id}"
    r = requests.get(
        url,
        headers={"x-api-key": api_key},
        params={"page": int(page), "page_size": int(page_size)},
        timeout=40,
    )
    r.raise_for_status()
    return r.json()


def _dedupe_citations(records: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for rec in records or []:
        for c in (rec.get("citations") or []):
            link = str(c.get("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            out.append(
                {
                    "title": str(c.get("title") or "").strip()[:180],
                    "link": link,
                    "published_date": str(c.get("published_date") or "").strip(),
                }
            )
            if len(out) >= limit:
                return out
    return out


def get_attack_benchmarks_cached(redis_conn) -> Optional[Dict[str, Any]]:
    try:
        raw = redis_conn.get("research:attack_benchmarks:v1")
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return None


def get_or_refresh_attack_benchmarks(redis_conn) -> Optional[Dict[str, Any]]:
    """
    Fetch market benchmarks via CatchAll (Newscatcher) with Redis caching.
    - Never raises (best-effort).
    - Uses short polling window; if job isn't ready, returns None.
    """
    try:
        api_key = str(os.getenv("CATCHALL_API_KEY", "") or "").strip()
        if not api_key:
            return None

        # Cache TTL (seconds)
        ttl_s = int(os.getenv("AUDIT_MARKET_CACHE_TTL_S", str(60 * 60 * 24 * 7)) or str(60 * 60 * 24 * 7))
        ttl_s = max(60 * 10, min(60 * 60 * 24 * 30, ttl_s))

        cached = get_attack_benchmarks_cached(redis_conn)
        if cached and (cached.get("fetched_at_utc") or ""):
            return cached

        job_key = "research:attack_benchmarks:job_id"
        job_id = (redis_conn.get(job_key) or b"").decode("utf-8", "ignore").strip()

        # If no pending job, submit a new one (lite mode, fewer records)
        if not job_id:
            now = _utc_now()
            start = _iso(now - timedelta(days=int(os.getenv("AUDIT_MARKET_DAYS", "30") or "30")))
            end = _iso(now)
            query = os.getenv(
                "AUDIT_MARKET_QUERY",
                "average cost of data breach ransomware incident downtime cost per hour cyber attack losses",
            )
            context = os.getenv(
                "AUDIT_MARKET_CONTEXT",
                "Extract USD costs, ranges, and what the estimate refers to (breach, ransomware, downtime, phishing).",
            )
            mode = os.getenv("AUDIT_MARKET_MODE", "lite")
            limit = int(os.getenv("AUDIT_MARKET_LIMIT", "10") or "10")
            limit = max(10, min(50, limit))
            job_id = catchall_submit_job(
                api_key=api_key, query=query, context=context, start_date=start, end_date=end, limit=limit, mode=mode
            )
            redis_conn.setex(job_key, 60 * 60, job_id)  # keep pending for 1h

        # Poll for a short window (we don't want audits to wait 10+ minutes)
        max_wait_s = int(os.getenv("AUDIT_MARKET_POLL_MAX_S", "45") or "45")
        max_wait_s = max(5, min(180, max_wait_s))
        interval_s = float(os.getenv("AUDIT_MARKET_POLL_INTERVAL_S", "6") or "6")
        interval_s = max(2.0, min(30.0, interval_s))

        deadline = time.time() + max_wait_s
        status = ""
        while time.time() < deadline:
            st = catchall_status(api_key=api_key, job_id=job_id) or {}
            status = str(st.get("status") or "").strip().lower()
            if status == "completed":
                break
            if status == "failed":
                redis_conn.delete(job_key)
                return None
            time.sleep(interval_s)

        if status != "completed":
            # Not ready yet; keep pending job in Redis for the next run.
            return None

        data = catchall_pull(api_key=api_key, job_id=job_id, page=1, page_size=50) or {}
        records = list(data.get("all_records") or [])
        citations = _dedupe_citations(records, limit=8)
        out = {
            "fetched_at_utc": _iso(_utc_now()),
            "job_id": job_id,
            "query": data.get("query"),
            "context": data.get("context"),
            "citations": citations,
        }
        redis_conn.setex("research:attack_benchmarks:v1", ttl_s, json.dumps(out, ensure_ascii=False))
        redis_conn.delete(job_key)
        return out
    except Exception:
        return None

