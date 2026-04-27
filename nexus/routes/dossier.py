from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from ..models import AuditRun, Site

bp = Blueprint("dossier", __name__)


@bp.get("/dossier/<audit_id>")
@login_required
def dossier(audit_id: str):
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    site = Site.query.filter_by(id=audit.site_id, org_id=current_user.org_id).first()
    return render_template("audit/dossier.html", audit=audit, site=site)

