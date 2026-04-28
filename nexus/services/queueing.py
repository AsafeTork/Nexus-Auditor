from __future__ import annotations

import os

import redis
from rq import Queue, Retry


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
    q = Queue("audits", connection=_redis_conn())
    job = q.enqueue(
        "nexus.worker.run_audit_job",
        audit_id,
        job_timeout=int(os.getenv("AUDIT_JOB_TIMEOUT_S", "1800")),
        ttl=int(os.getenv("AUDIT_JOB_TTL_S", "3600")),  # drop if stuck in queue too long
        result_ttl=0,
        failure_ttl=int(os.getenv("AUDIT_JOB_FAILURE_TTL_S", "86400")),
        retry=Retry(max=1, interval=[30]),
    )
    return job.id


def enqueue_ui_lab(run_id: str, org_id: str, mode: str, payload: dict) -> str:
    """
    Enqueue an UI-Lab review job (admin UX suggestions).
    """
    q = Queue("ui", connection=_redis_conn())
    job = q.enqueue(
        "nexus.worker.run_ui_lab_job",
        run_id,
        org_id,
        mode,
        payload,
        job_timeout=int(os.getenv("UI_JOB_TIMEOUT_S", "900")),
        ttl=int(os.getenv("UI_JOB_TTL_S", "1800")),
        result_ttl=int(os.getenv("UI_JOB_RESULT_TTL_S", str(60 * 60 * 24 * 7))),
        failure_ttl=int(os.getenv("UI_JOB_FAILURE_TTL_S", "86400")),
        retry=Retry(max=1, interval=[30]),
    )
    return job.id
