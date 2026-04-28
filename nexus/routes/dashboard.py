from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, session
from flask_login import login_required, current_user

from ..models import Site, AuditRun, Subscription

bp = Blueprint("dashboard", __name__)


@bp.get("/")
@login_required
def home():
    sites = Site.query.filter_by(org_id=current_user.org_id).order_by(Site.created_utc.desc()).limit(20).all()
    audits = AuditRun.query.filter_by(org_id=current_user.org_id).order_by(AuditRun.created_utc.desc()).limit(50).all()
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    # Admin simulator (session-only)
    if current_user.is_admin:
        sim_sub = (session.get("sim_sub_status") or "").strip().lower()
        if sim_sub:
            sub = sub or Subscription(org_id=current_user.org_id)
            sub.status = sim_sub

    # KPIs
    sites_count = len(sites)
    total_audits = len(audits)
    status_counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
    for a in audits:
        status_counts[str(a.status or "queued")] = status_counts.get(str(a.status or "queued"), 0) + 1

    # Simple 7-day trend (based on created_utc ISO strings)
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    labels = [d.strftime("%d/%m") for d in days]
    counts = [0 for _ in days]
    for a in audits:
        try:
            d = datetime.fromisoformat(a.created_utc).date()
            if d in days:
                counts[days.index(d)] += 1
        except Exception:
            pass

    return render_template(
        "dashboard/home.html",
        sites=sites,
        audits=audits,
        sub=sub,
        kpi={
            "sites": sites_count,
            "audits": total_audits,
            "done": status_counts.get("done", 0),
            "errors": status_counts.get("error", 0),
        },
        trend={"labels": labels, "counts": counts},
        status_counts=status_counts,
    )
