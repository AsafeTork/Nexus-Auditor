from __future__ import annotations

import os

import redis
from rq import Queue


def _redis_conn():
    """
    Fast Redis connection resolver.
    Do NOT instantiate a Flask app just to read REDIS_URL.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(redis_url)


def enqueue_audit(audit_id: str) -> str:
    """
    Enqueue an audit job in RQ (Redis Queue).
    """
    q = Queue("audits", connection=_redis_conn(), default_timeout=1800)
    job = q.enqueue("nexus.worker.run_audit_job", audit_id)
    return job.id


def enqueue_ui_lab(run_id: str, org_id: str, mode: str, payload: dict) -> str:
    """
    Enqueue an UI-Lab review job (admin UX suggestions).
    """
    q = Queue("ui", connection=_redis_conn(), default_timeout=1800)
    job = q.enqueue("nexus.worker.run_ui_lab_job", run_id, org_id, mode, payload)
    return job.id
