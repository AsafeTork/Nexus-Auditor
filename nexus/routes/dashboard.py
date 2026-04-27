from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from ..models import Site, AuditRun, Subscription

bp = Blueprint("dashboard", __name__)


@bp.get("/")
@login_required
def home():
    sites = Site.query.filter_by(org_id=current_user.org_id).order_by(Site.created_utc.desc()).limit(20).all()
    audits = AuditRun.query.filter_by(org_id=current_user.org_id).order_by(AuditRun.created_utc.desc()).limit(20).all()
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    return render_template("dashboard/home.html", sites=sites, audits=audits, sub=sub)

