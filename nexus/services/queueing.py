from __future__ import annotations

import os

import redis
from rq import Queue

from .. import create_app


def _redis_conn():
    app = create_app()
    return redis.from_url(app.config["REDIS_URL"])


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
