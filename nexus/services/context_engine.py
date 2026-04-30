from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Dict

from .. import db
from ..models import AuditRun, MonitoringFinding, MonitoringJob, MonitoringRun, SiteContext


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clamp(x: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, int(x)))


def load_site_context(org_id: str, site_id: str) -> SiteContext:
    ctx = SiteContext.query.filter_by(org_id=org_id, site_id=site_id).first()
    if ctx:
        return ctx
    ctx = SiteContext(org_id=org_id, site_id=site_id, complexity="MEDIUM", coverage_quality="MEDIUM", instability_score=0)
    db.session.add(ctx)
    db.session.commit()
    return ctx


def derive_coverage_quality(audit: AuditRun) -> str:
    """
    Deterministic proxy for scan coverage quality.
    Signals:
      - audit status
      - presence of "Fetching HTML" logs
      - obvious error keywords in logs
    """
    logs = (audit.logs or "").lower()
    ok_fetch = "fetching html:" in logs
    has_errors = any(k in logs for k in ("httperror", "traceback", "exception", "timeout", "error:"))

    if str(audit.status or "").lower() != "done":
        return "LOW"
    if ok_fetch and (not has_errors):
        return "HIGH"
    return "MEDIUM"


def compute_instability_score(job: MonitoringJob, lookback: int = 20) -> int:
    """
    0..100 score combining:
      - regression rate (per monitoring_findings)
      - error ratio (recent monitoring runs)
    """
    lookback = max(5, min(100, int(lookback or 20)))

    # Error ratio from recent runs
    runs = (
        MonitoringRun.query.filter_by(job_id=job.id)
        .order_by(MonitoringRun.created_utc.desc())
        .limit(lookback)
        .all()
    )
    total = len(runs) or 1
    err = sum(1 for r in runs if str(r.status or "").lower() == "error")
    err_ratio = err / total

    # Regression rate from lifecycle table
    rows = MonitoringFinding.query.filter_by(job_id=job.id).limit(5000).all()
    regs = sum(int(r.regression_count or 0) for r in rows)
    resolved = sum(1 for r in rows if str(r.state or "") == "RESOLVED")
    reg_ratio = regs / max(1, resolved)

    # Weighted (0..100)
    score = int(round(60.0 * err_ratio + 40.0 * min(1.0, reg_ratio)))
    return _clamp(score)  # keep 0..100


def context_snapshot(*, job: MonitoringJob, audit: AuditRun) -> Dict:
    """
    Update and return a context dict used by safety gate and verification loop.
    """
    ctx = load_site_context(job.org_id, job.site_id)

    # Operator configurable complexity (LOW/MEDIUM/HIGH)
    complexity = (ctx.complexity or "MEDIUM").strip().upper()
    if complexity not in ("LOW", "MEDIUM", "HIGH"):
        complexity = "MEDIUM"

    coverage = derive_coverage_quality(audit)
    instability = compute_instability_score(job, lookback=int(os.getenv("CONTEXT_LOOKBACK", "20") or "20"))

    ctx.coverage_quality = coverage
    ctx.instability_score = int(instability)
    ctx.last_updated_utc = _utc_iso()
    db.session.commit()

    # Derive strictness (0..100)
    strictness = 0
    if complexity == "HIGH":
        strictness += 35
    elif complexity == "MEDIUM":
        strictness += 15
    # low coverage increases strictness heavily (prevents false safe/false resolved)
    if coverage == "LOW":
        strictness += 45
    elif coverage == "MEDIUM":
        strictness += 20
    # instability contributes linearly
    strictness += int(instability * 0.4)
    strictness = _clamp(strictness)

    return {
        "complexity": complexity,
        "coverage_quality": coverage,
        "instability_score": int(instability),
        "strictness": strictness,
        "explain": {
            "rules": [
                "HIGH complexity increases strictness (risk of breaking changes).",
                "LOW coverage blocks confident conclusions (avoid false resolved).",
                "High instability/regressions increases strictness.",
            ]
        },
    }
