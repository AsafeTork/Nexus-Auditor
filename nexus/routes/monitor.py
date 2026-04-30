from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, url_for, flash
from flask_login import login_required, current_user

from .. import db
from ..models import MonitoringFinding, MonitoringJob, MonitoringRun, Site, SiteContext
from ..security import require_admin
from ..services.monitoring import ensure_monitor_job, enqueue_due_monitoring_runs


bp = Blueprint("monitor", __name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@bp.post("/admin/monitor/tick")
def monitor_tick():
    """
    Token-protected endpoint intended for external cron (Render cron job).
    Enqueues due monitoring jobs (at-least-once).
    """
    token = (request.args.get("token") or request.headers.get("X-Monitor-Token") or "").strip()
    expected = (os.getenv("MONITOR_TICK_TOKEN", "") or "").strip()
    if not expected or token != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit") or "50")
    except Exception:
        limit = 50

    enq, audit_ids = enqueue_due_monitoring_runs(limit=limit)
    return jsonify({"ok": True, "ts_utc": _utc_now_iso(), "enqueued": enq, "audit_ids": audit_ids})


@bp.get("/admin/monitoring")
@login_required
@require_admin
def monitoring_home():
    """
    Minimal monitoring management UI (per org).
    """
    sites = Site.query.filter_by(org_id=current_user.org_id).order_by(Site.created_utc.desc()).limit(200).all()
    jobs = MonitoringJob.query.filter_by(org_id=current_user.org_id).all()
    job_map = {j.site_id: j for j in jobs}
    contexts = SiteContext.query.filter_by(org_id=current_user.org_id).all()
    ctx_map = {c.site_id: c for c in contexts}

    # Lightweight history: last 30 runs across org
    runs = (
        MonitoringRun.query.filter_by(org_id=current_user.org_id)
        .order_by(MonitoringRun.created_utc.desc())
        .limit(30)
        .all()
    )

    # Org-level aggregates (lightweight, deterministic)
    total_resolved = MonitoringFinding.query.filter_by(org_id=current_user.org_id, state="RESOLVED").count()
    total_open = (
        MonitoringFinding.query.filter_by(org_id=current_user.org_id)
        .filter(MonitoringFinding.state.in_(["NEW", "PERSISTING", "REOPENED"]))
        .count()
    )
    denom = max(1, total_resolved + total_open)
    fix_success_rate = round((total_resolved / denom) * 100.0, 2)
    rows = (
        db.session.query(MonitoringFinding.resolution_time_s)
        .filter_by(org_id=current_user.org_id, state="RESOLVED")
        .filter(MonitoringFinding.resolution_time_s > 0)
        .limit(5000)
        .all()
    )
    times = [int(r[0]) for r in rows if r and r[0] is not None]
    avg_time_s = int(sum(times) / len(times)) if times else 0

    return render_template(
        "admin/monitoring.html",
        sites=sites,
        job_map=job_map,
        ctx_map=ctx_map,
        runs=runs,
        fix_success_rate=fix_success_rate,
        avg_time_s=avg_time_s,
        total_resolved=total_resolved,
        total_open=total_open,
    )


@bp.post("/admin/monitoring/site/<site_id>/save")
@login_required
@require_admin
def monitoring_save(site_id: str):
    site = Site.query.filter_by(id=site_id, org_id=current_user.org_id).first_or_404()
    job = ensure_monitor_job(current_user.org_id, site.id)
    ctx = SiteContext.query.filter_by(org_id=current_user.org_id, site_id=site.id).first()
    if not ctx:
        ctx = SiteContext(org_id=current_user.org_id, site_id=site.id)
        db.session.add(ctx)

    enabled = (request.form.get("enabled") or "").strip().lower() in ("1", "true", "yes", "on")
    mode = (request.form.get("mode") or "full").strip().lower()
    if mode not in ("full", "fast"):
        mode = "full"

    try:
        freq_min = int((request.form.get("frequency_min") or "60").strip())
    except Exception:
        freq_min = 60
    freq_min = max(1, min(7 * 24 * 60, freq_min))

    job.enabled = bool(enabled)
    job.mode = mode
    job.frequency_s = int(freq_min * 60)
    job.updated_utc = _utc_now_iso()

    complexity = (request.form.get("complexity") or "MEDIUM").strip().upper()
    if complexity not in ("LOW", "MEDIUM", "HIGH"):
        complexity = "MEDIUM"
    ctx.complexity = complexity
    ctx.last_updated_utc = _utc_now_iso()

    # If enabled and next_run is empty, schedule the first run soon.
    if job.enabled and not (job.next_run_utc or "").strip():
        job.next_run_utc = _utc_now_iso()

    db.session.commit()
    flash("Monitoring settings saved.", "ok")
    return redirect(url_for("monitor.monitoring_home"))
