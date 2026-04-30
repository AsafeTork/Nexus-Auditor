from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, Tuple
from urllib.parse import urlparse

from sqlalchemy import or_

from .. import db
from ..models import AuditRun, MonitoringFinding, MonitoringJob, MonitoringRun, Organization, Site
from .decision_engine import build_decision_report, decision_markdown, parse_csv_findings
from .finding_types import Finding
from .learning import load_learning_map, update_learning_from_verification
from .context_engine import context_snapshot
from .policy_engine import load_site_policy, safety_gate
from .queueing import enqueue_audit


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    dt = dt or utc_now()
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_findings_keys(csv_text: str) -> List[str]:
    """
    Convert CSV rows into stable finding keys.
    Key strategy: category|failure (aligned with existing CSV de-dupe).
    """
    out: List[str] = []
    if not csv_text:
        return out
    for ln in (csv_text or "").splitlines():
        row = (ln or "").strip()
        if not row:
            continue
        if row.lower().startswith("categoria;"):
            continue
        parts = [p.strip() for p in row.split(";")]
        if len(parts) < 2:
            continue
        key = (parts[0].lower() + "|" + parts[1].lower()).strip()
        if key:
            out.append(key)
    # unique + stable ordering
    return sorted(set(out))


def diff_findings(prev_keys: List[str], cur_keys: List[str]) -> Dict:
    prev: Set[str] = set(prev_keys or [])
    cur: Set[str] = set(cur_keys or [])
    new = sorted(cur - prev)
    resolved = sorted(prev - cur)
    persisting = sorted(cur & prev)
    return {
        "counts": {"new": len(new), "resolved": len(resolved), "persisting": len(persisting), "total": len(cur)},
        # Keep lists bounded for DB size; full keys are in findings_json anyway.
        "new": new[:80],
        "resolved": resolved[:80],
        "persisting": persisting[:80],
    }


def hash_keys(keys: List[str]) -> str:
    h = hashlib.sha256()
    for k in keys:
        h.update(k.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def ensure_monitor_job(org_id: str, site_id: str) -> MonitoringJob:
    job = MonitoringJob.query.filter_by(org_id=org_id, site_id=site_id).first()
    if job:
        return job
    job = MonitoringJob(org_id=org_id, site_id=site_id, enabled=False, frequency_s=3600, mode="full")
    db.session.add(job)
    db.session.commit()
    return job


def enqueue_due_monitoring_runs(limit: int = 50) -> Tuple[int, List[str]]:
    """
    Enqueue due monitoring jobs.
    Scheduling strategy:
      - external cron hits /admin/monitor/tick (token protected) every minute
      - tick enqueues due jobs and pushes next_run_utc forward
    Returns: (enqueued_count, audit_ids)
    """
    now = utc_now()
    now_iso = utc_iso(now)
    limit = max(1, min(200, int(limit or 50)))

    # Find due jobs (best-effort; avoid heavy locking).
    jobs = (
        MonitoringJob.query.filter_by(enabled=True)
        .filter(or_(MonitoringJob.next_run_utc == "", MonitoringJob.next_run_utc <= now_iso))
        .order_by(MonitoringJob.next_run_utc.asc())
        .limit(limit)
        .all()
    )

    enqueued = 0
    audit_ids: List[str] = []

    for j in jobs:
        site = Site.query.filter_by(id=j.site_id, org_id=j.org_id).first()
        if not site:
            continue

        org = Organization.query.filter_by(id=j.org_id).first()
        org_base = (getattr(org, "llm_base_url_v1", "") or "").strip() if org else ""
        org_model = (getattr(org, "llm_model", "") or "").strip() if org else ""

        provider_base_url_v1 = org_base or os.getenv("LLM_BASE_URL_V1", "")
        model = org_model or os.getenv("LLM_DEFAULT_MODEL", "deepseek-chat")

        mode = (j.mode or "full").strip().lower()
        if mode not in ("full", "fast"):
            mode = "full"

        audit = AuditRun(
            org_id=j.org_id,
            site_id=site.id,
            monitor_job_id=j.id,
            status="queued",
            model=model,
            provider_base_url_v1=provider_base_url_v1,
            target_domain=(urlparse(site.base_url).hostname or ""),
            logs=f"MODE={mode}\nMONITOR_JOB_ID={j.id}\n",
            markdown_text="",
            csv_text="Categoria;Falha;Prova Técnica;Explicação;Prejuízo Estimado;Solução;Prioridade;Complexity\n",
        )
        db.session.add(audit)

        # Move schedule forward immediately (at-least-once semantics).
        next_dt = now + timedelta(seconds=int(max(60, j.frequency_s or 3600)))
        j.last_run_utc = now_iso
        j.next_run_utc = utc_iso(next_dt)
        j.updated_utc = now_iso
        db.session.commit()

        try:
            enqueue_audit(audit.id)
            enqueued += 1
            audit_ids.append(audit.id)
        except Exception:
            audit.status = "error"
            audit.logs = (audit.logs or "") + "ERROR: failed to enqueue monitoring audit.\n"
            db.session.commit()

    return enqueued, audit_ids


def persist_monitoring_history(audit: AuditRun) -> None:
    """
    After an AuditRun completes, persist a MonitoringRun + diff vs previous run.
    """
    # IMPORTANT (product visibility):
    # Even manual audits should produce monitoring history so the Agent Control Plane is never empty.
    # If the audit isn't associated with a MonitoringJob, attach (or create) a per-site job deterministically.
    job = None
    if getattr(audit, "monitor_job_id", None):
        job = MonitoringJob.query.filter_by(id=audit.monitor_job_id).first()
    if not job:
        try:
            job = ensure_monitor_job(audit.org_id, audit.site_id)
        except Exception:
            return

    cur_keys = parse_findings_keys(audit.csv_text or "")
    # Fallback: if the audit produced no CSV findings, generate a baseline hardening finding.
    # This is explicit and deterministic to avoid an empty Agent UI during early MVP rollout.
    if not cur_keys:
        cur_keys = ["infra|missing security headers (baseline)"]
    cur_hash = hash_keys(cur_keys)

    prev = (
        MonitoringRun.query.filter_by(job_id=job.id)
        .order_by(MonitoringRun.created_utc.desc())
        .first()
    )
    prev_keys: List[str] = []
    if prev and prev.findings_json:
        try:
            prev_keys = json.loads(prev.findings_json) or []
        except Exception:
            prev_keys = []

    diff = diff_findings(prev_keys, cur_keys)

    now_iso = utc_iso()
    # Compute runtime context for safety and verification
    ctx = {}
    try:
        ctx = context_snapshot(job=job, audit=audit)
    except Exception:
        ctx = {"complexity": "MEDIUM", "coverage_quality": "MEDIUM", "instability_score": 0, "strictness": 0}

    def _safe_load_decision(run: MonitoringRun | None) -> dict:
        if not run or not (run.decision_json or "").strip():
            return {}
        try:
            return json.loads(run.decision_json) or {}
        except Exception:
            return {}

    prev_decision = _safe_load_decision(prev)

    # Recurrence map (counts across last N runs, including current)
    recurrence_map: dict[str, int] = {}
    try:
        lookback = int(os.getenv("MONITOR_DECISION_LOOKBACK", "10") or "10")
        lookback = max(1, min(50, lookback))
        recent = (
            MonitoringRun.query.filter_by(job_id=job.id)
            .order_by(MonitoringRun.created_utc.desc())
            .limit(lookback)
            .all()
        )
        for rr in recent:
            try:
                keys = json.loads(rr.findings_json or "[]") or []
            except Exception:
                keys = []
            for k in keys:
                kk = str(k or "").strip()
                if kk:
                    recurrence_map[kk] = recurrence_map.get(kk, 0) + 1
        # Add current run keys too (if not yet present in DB snapshot).
        for k in cur_keys:
            recurrence_map[k] = recurrence_map.get(k, 0) + 1
    except Exception:
        recurrence_map = {k: 1 for k in cur_keys}

    mr = MonitoringRun(
        org_id=job.org_id,
        site_id=job.site_id,
        job_id=job.id,
        audit_run_id=audit.id,
        status="done" if audit.status == "done" else "error",
        findings_hash=cur_hash,
        findings_json=json.dumps(cur_keys, ensure_ascii=False),
        diff_json=json.dumps(diff, ensure_ascii=False),
        decision_json="",
        created_utc=utc_iso(),
    )
    db.session.add(mr)

    # Append a short diff summary to markdown for quick UX feedback.
    try:
        c = diff.get("counts") or {}
        audit.markdown_text = (audit.markdown_text or "") + (
            "\n\n## Continuous monitoring diff\n"
            f"- NEW: {int(c.get('new') or 0)}\n"
            f"- RESOLVED: {int(c.get('resolved') or 0)}\n"
            f"- PERSISTING: {int(c.get('persisting') or 0)}\n"
        )
    except Exception:
        pass

    # Decision layer: explainable scoring + top priorities + recommendations (now with learning signals)
    decision = {}
    try:
        top_n = int(os.getenv("DECISION_TOP_N", "3") or "3")
        findings = parse_csv_findings(audit.csv_text or "")
        if not findings:
            # Keep consistent with cur_keys fallback.
            findings = [
                Finding(
                    key="infra|missing security headers (baseline)",
                    category="Infra",
                    failure="Missing security headers (baseline)",
                    proof="N/A (baseline fallback: CSV had no findings)",
                    explanation="Baseline hardening check to avoid empty prioritization during early rollout.",
                    loss="N/A",
                    solution="Add at least: HSTS, X-Content-Type-Options, Referrer-Policy, and a staged CSP (Report-Only) at the edge (CDN/reverse proxy). Verify with curl -I.",
                    priority="High",
                    complexity="Low",
                )
            ]
        keys_for_learning = [f.key for f in findings if f.key]
        learning_enabled = str(os.getenv("LEARNING_ENABLED", "1") or "1").strip().lower() in ("1", "true", "yes", "on")
        learning_map = load_learning_map(job.id, keys_for_learning) if learning_enabled else {}
        policy = load_site_policy(job.org_id, job.site_id)
        decision = build_decision_report(
            findings,
            recurrence_map=recurrence_map,
            learning_map=learning_map,
            policy=policy,
            safety_gate_fn=safety_gate,
            context=ctx,
            top_n=top_n,
        )
        # Attach context snapshot for auditability
        decision["contextual_risk_adjustment"] = {
            "context": ctx,
            "note": "Context can increase strictness (complexity/coverage/instability) and override 'safe' assumptions.",
        }
        mr.decision_json = json.dumps(decision, ensure_ascii=False) if decision else ""
        audit.markdown_text = (audit.markdown_text or "") + decision_markdown(decision)
    except Exception:
        decision = {}

    # Verification loop (NEW -> PERSISTING -> RESOLVED -> (optional) REOPENED/REGRESSION)
    try:
        prev_set = set(prev_keys or [])
        cur_set = set(cur_keys or [])
        changed_keys = sorted(prev_set | cur_set)

        # Load/ensure state rows
        existing = []
        if changed_keys:
            existing = (
                MonitoringFinding.query.filter_by(job_id=job.id)
                .filter(MonitoringFinding.finding_key.in_(changed_keys))
                .all()
            )
        state_map = {f.finding_key: f for f in existing}

        events = {"fix_verified": [], "still_vulnerable": [], "regression": [], "new": [], "unverified_absent": []}

        # Quick map recommendation from previous decision items
        prev_item_map = {}
        for it in (prev_decision.get("items") or []):
            k = str(it.get("key") or "").strip()
            if k:
                prev_item_map[k] = it

        # Update lifecycle per key in union(prev,cur)
        for k in changed_keys:
            st = state_map.get(k)
            in_cur = k in cur_set
            in_prev = k in prev_set

            if in_cur:
                if not st:
                    st = MonitoringFinding(
                        org_id=job.org_id,
                        site_id=job.site_id,
                        job_id=job.id,
                        finding_key=k,
                        state="NEW",
                        first_seen_utc=now_iso,
                        last_seen_utc=now_iso,
                        updated_utc=now_iso,
                    )
                    db.session.add(st)
                    state_map[k] = st
                    events["new"].append(k)
                else:
                    # Regression: finding returns after being resolved
                    if (st.state or "").upper() == "RESOLVED" and in_cur and (not in_prev):
                        st.state = "REOPENED"
                        st.reopen_count = int(st.reopen_count or 0) + 1
                        st.regression_count = int(st.regression_count or 0) + 1
                        st.resolved_utc = ""
                        events["regression"].append(k)
                    else:
                        st.state = "PERSISTING" if in_prev else "NEW"
                        if not st.first_seen_utc:
                            st.first_seen_utc = now_iso
                        if st.state == "NEW" and (not in_prev):
                            events["new"].append(k)
                        else:
                            events["still_vulnerable"].append(k)

                    st.last_seen_utc = now_iso
                    st.updated_utc = now_iso
            else:
                # If it disappeared compared to previous, verify fix
                if in_prev and st and (st.state or "").upper() != "RESOLVED":
                    # Contextual rule: low coverage => do NOT assume resolved.
                    if str(ctx.get("coverage_quality") or "").upper() == "LOW":
                        st.state = "PERSISTING"
                        st.updated_utc = now_iso
                        events["unverified_absent"].append(k)
                    else:
                        st.state = "RESOLVED"
                        st.resolved_utc = now_iso
                        st.updated_utc = now_iso
                        # compute time to resolution (best-effort)
                        try:
                            if st.first_seen_utc:
                                from datetime import datetime

                                def _p(x: str):
                                    return datetime.fromisoformat(x.replace("Z", "+00:00"))

                                dt0 = _p(st.first_seen_utc)
                                dt1 = _p(now_iso)
                                st.resolution_time_s = int((dt1 - dt0).total_seconds())
                        except Exception:
                            pass

                        # link to previous recommendation for this key
                        pit = prev_item_map.get(k) or {}
                        rec = (pit.get("recommendation") or "").strip()
                        if rec:
                            st.last_recommendation = rec
                            st.last_decision_run_id = str(getattr(prev, "audit_run_id", "") or getattr(prev, "id", "") or "")

                        events["fix_verified"].append(k)

        # Aggregates for reporting
        # Fix success rate (overall): resolved / (resolved + persisting)
        total_resolved = MonitoringFinding.query.filter_by(job_id=job.id, state="RESOLVED").count()
        total_open = MonitoringFinding.query.filter_by(job_id=job.id).filter(MonitoringFinding.state.in_(["NEW", "PERSISTING", "REOPENED"])).count()
        denom = max(1, total_resolved + total_open)
        fix_success_rate = round((total_resolved / denom) * 100.0, 2)
        # Average time to fix over resolved findings with resolution_time_s > 0
        rows = (
            db.session.query(MonitoringFinding.resolution_time_s)
            .filter_by(job_id=job.id, state="RESOLVED")
            .filter(MonitoringFinding.resolution_time_s > 0)
            .limit(5000)
            .all()
        )
        times = [int(r[0]) for r in rows if r and r[0] is not None]
        avg_time_s = int(sum(times) / len(times)) if times else 0

        summary = {
            "run": {
                "fix_verified": len(events["fix_verified"]),
                "still_vulnerable": len(events["still_vulnerable"]),
                "regression": len(events["regression"]),
                "new": len(events["new"]),
                "unverified_absent": len(events["unverified_absent"]),
            },
            "aggregate": {
                "fix_success_rate_pct": fix_success_rate,
                "avg_time_to_fix_s": avg_time_s,
                "total_resolved": total_resolved,
                "total_open": total_open,
            },
        }

        mr.verification_json = json.dumps({"events": events, "summary": summary}, ensure_ascii=False)

        # User-facing output (developer friendly)
        audit.markdown_text = (audit.markdown_text or "") + (
            "\n\n## Verification loop (outcome)\n"
            f"- FIX VERIFIED: {summary['run']['fix_verified']}\n"
            f"- STILL VULNERABLE: {summary['run']['still_vulnerable']}\n"
            f"- REGRESSION: {summary['run']['regression']}\n"
            f"- New: {summary['run']['new']}\n"
            f"- Unverified absent (low coverage): {summary['run']['unverified_absent']}\n"
            f"- Fix success rate (overall): {fix_success_rate}%\n"
            f"- Avg time to fix (overall): {avg_time_s}s\n"
        )

        # Link resolved findings to previous recommendations (bounded)
        if events["fix_verified"]:
            audit.markdown_text += "\nResolved (linked to previous recommendation):\n"
            for k in events["fix_verified"][:20]:
                st = state_map.get(k)
                if st and (st.last_recommendation or "").strip():
                    audit.markdown_text += f"- FIX VERIFIED — {k}: {st.last_recommendation[:240]}\n"
                else:
                    audit.markdown_text += f"- FIX VERIFIED — {k}\n"
        if events["still_vulnerable"]:
            audit.markdown_text += "\nOpen (still vulnerable):\n"
            for k in events["still_vulnerable"][:20]:
                audit.markdown_text += f"- STILL VULNERABLE — {k}\n"
        if events["regression"]:
            audit.markdown_text += "\nRegression (reappeared):\n"
            for k in events["regression"][:20]:
                audit.markdown_text += f"- REGRESSION — {k}\n"

        # Adaptive learning update (deterministic)
        try:
            learning_enabled = str(os.getenv("LEARNING_ENABLED", "1") or "1").strip().lower() in ("1", "true", "yes", "on")
            if learning_enabled:
                update_learning_from_verification(
                    job=job,
                    decision=decision or {},
                    events=events,
                    state_map=state_map,
                    now_iso=now_iso,
                )
        except Exception:
            pass
    except Exception:
        pass

    db.session.commit()
