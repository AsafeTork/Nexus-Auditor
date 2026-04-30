from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Tuple

from .. import db
from ..models import LearningStat, MonitoringFinding, MonitoringJob


def classify_rec_kind(recommendation: str, category: str) -> str:
    """
    Deterministic, explainable classification of recommendation strategy.
    We avoid hashing freeform LLM output to reduce noise.
    """
    rec = (recommendation or "").lower()
    cat = (category or "").lower()

    if not rec:
        return "unknown"
    if "strict-transport-security" in rec or "hsts" in rec:
        return "headers_hsts"
    if "content-security-policy" in rec or "csp" in rec:
        return "headers_csp"
    if "cors" in rec:
        return "cors_policy"
    if "parameterized" in rec or "prepared statement" in rec:
        return "sqli_param_queries"
    if "output encoding" in rec or "escape" in rec:
        return "xss_output_encoding"
    if "rate limit" in rec or "throttle" in rec:
        return "rate_limit"
    if "csrf" in rec:
        return "csrf_protection"

    if "headers" in cat or "infra" in cat or "ssl" in cat or "tls" in cat:
        return "infra_generic"
    if "segurança" in cat or "security" in cat or "vulnerab" in cat:
        return "security_generic"
    return "generic"


def _ewma(prev: int, new: int, alpha: float = 0.2) -> int:
    if prev <= 0:
        return int(new)
    return int(round((1.0 - alpha) * float(prev) + alpha * float(new)))


def load_learning_map(job_id: str, keys: list[str]) -> Dict[str, dict]:
    """
    Returns {finding_key: {success_rate, avg_resolution_s, regression_rate, sample_size, rec_kind}}
    """
    if not keys:
        return {}
    rows = (
        LearningStat.query.filter_by(job_id=job_id)
        .filter(LearningStat.finding_key.in_(keys))
        .all()
    )
    out: Dict[str, dict] = {}
    for r in rows:
        seen = int(r.seen_count or 0)
        resolved = int(r.resolved_count or 0)
        open_count = int(r.open_count or 0)
        reg = int(r.regression_count or 0)
        denom = max(1, resolved + open_count)
        success_rate = resolved / denom
        regression_rate = reg / max(1, resolved)
        out[str(r.finding_key)] = {
            "success_rate": round(success_rate, 4),
            "avg_resolution_s": int(r.avg_resolution_s or 0),
            "regression_rate": round(regression_rate, 4),
            "sample_size": int(seen),
            "rec_kind": str(r.rec_kind or "unknown"),
        }
    return out


def update_learning_from_verification(
    *,
    job: MonitoringJob,
    decision: dict,
    events: dict,
    state_map: dict,
    now_iso: str,
) -> None:
    """
    Update learning store deterministically after a run.
    - seen/open increments for keys present in current run
    - resolved increments for fix_verified
    - regression increments for regressions
    - avg_resolution_s updated from MonitoringFinding.resolution_time_s
    """
    items = decision.get("items") or []
    item_map = {str(it.get("key") or "").strip(): it for it in items if (it.get("key") or "").strip()}

    def _get_or_create(key: str, rec_kind: str) -> LearningStat:
        st = LearningStat.query.filter_by(job_id=job.id, finding_key=key).first()
        if st:
            # keep the first non-unknown kind; otherwise update
            if (st.rec_kind or "unknown") == "unknown" and rec_kind != "unknown":
                st.rec_kind = rec_kind
            return st
        st = LearningStat(org_id=job.org_id, job_id=job.id, finding_key=key, rec_kind=rec_kind)
        db.session.add(st)
        db.session.flush()
        return st

    # Keys present in current run: seen/open
    for key, it in item_map.items():
        rec_kind = classify_rec_kind(str(it.get("recommendation") or ""), str(it.get("category") or ""))
        st = _get_or_create(key, rec_kind)
        st.seen_count = int(st.seen_count or 0) + 1
        st.open_count = int(st.open_count or 0) + 1
        st.updated_utc = now_iso

    # Fix verified: resolved, remove one from open_count if possible
    for key in (events.get("fix_verified") or []):
        key = str(key or "").strip()
        if not key:
            continue
        it = item_map.get(key) or {}
        rec_kind = classify_rec_kind(str(it.get("recommendation") or ""), str(it.get("category") or ""))
        st = _get_or_create(key, rec_kind)
        st.resolved_count = int(st.resolved_count or 0) + 1
        st.open_count = max(0, int(st.open_count or 0) - 1)
        st.updated_utc = now_iso
        mf = state_map.get(key)
        if mf and int(getattr(mf, "resolution_time_s", 0) or 0) > 0:
            st.avg_resolution_s = _ewma(int(st.avg_resolution_s or 0), int(mf.resolution_time_s or 0))

    # Regression: penalize (counts as a failure signal)
    for key in (events.get("regression") or []):
        key = str(key or "").strip()
        if not key:
            continue
        st = LearningStat.query.filter_by(job_id=job.id, finding_key=key).first()
        if not st:
            st = LearningStat(org_id=job.org_id, job_id=job.id, finding_key=key, rec_kind="unknown")
            db.session.add(st)
        st.regression_count = int(st.regression_count or 0) + 1
        st.updated_utc = now_iso

