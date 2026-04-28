from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for, stream_with_context
from flask_login import login_required, current_user
from flask import session

from .. import db
from ..models import AuditEvent, AuditRun, Site, Subscription, is_subscription_active
from ..security import require_admin
from ..services.queueing import enqueue_audit

bp = Blueprint("audit", __name__)


@bp.post("/sites")
@login_required
def create_site():
    name = (request.form.get("name") or "").strip() or "Site"
    base_url = (request.form.get("base_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    site = Site(org_id=current_user.org_id, name=name, base_url=base_url)
    db.session.add(site)
    db.session.commit()
    return redirect(url_for("dashboard.home"))


@bp.post("/sites/<site_id>/delete")
@login_required
@require_admin
def delete_site(site_id: str):
    """
    Delete a site and all associated audits/events (org-scoped).
    """
    site = Site.query.filter_by(id=site_id, org_id=current_user.org_id).first_or_404()
    try:
        audits = AuditRun.query.filter_by(site_id=site.id, org_id=current_user.org_id).all()
        audit_ids = [a.id for a in audits]
        if audit_ids:
            AuditEvent.query.filter(AuditEvent.audit_run_id.in_(audit_ids)).delete(synchronize_session=False)
            AuditRun.query.filter(AuditRun.id.in_(audit_ids)).delete(synchronize_session=False)
        db.session.delete(site)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return redirect(url_for("dashboard.home"))


@bp.post("/start")
@login_required
def start_audit():
    site_id = (request.form.get("site_id") or "").strip()
    site = Site.query.filter_by(id=site_id, org_id=current_user.org_id).first_or_404()

    # Avoid accidental duplicate runs: if there is a recent queued/running audit, reuse it.
    try:
        existing = (
            AuditRun.query.filter_by(org_id=current_user.org_id, site_id=site.id)
            .filter(AuditRun.status.in_(["queued", "running"]))
            .order_by(AuditRun.created_utc.desc())
            .first()
        )
        if existing:
            try:
                created = datetime.fromisoformat(existing.created_utc)
            except Exception:
                created = None
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created and created > datetime.now(timezone.utc) - timedelta(hours=6):
                return redirect(url_for("audit.view_audit", audit_id=existing.id))
    except Exception:
        pass

    # Feature gating: require active subscription (trialing counts as active).
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    if current_user.is_admin:
        sim_sub = (session.get("sim_sub_status") or "").strip().lower()
        if sim_sub:
            sub = sub or Subscription(org_id=current_user.org_id)
            sub.status = sim_sub
    if not is_subscription_active(sub):
        return redirect(url_for("dashboard.home", billing="required"))

    provider_base_url_v1 = (request.form.get("provider_base_url_v1") or "").strip() or os.getenv("LLM_BASE_URL_V1", "")
    model = (request.form.get("model") or "").strip() or os.getenv("LLM_DEFAULT_MODEL", "deepseek-chat")
    if not provider_base_url_v1:
        provider_base_url_v1 = "https://eclipse.mestredoblack.pro/v1"

    audit = AuditRun(
        org_id=current_user.org_id,
        site_id=site.id,
        status="queued",
        model=model,
        provider_base_url_v1=provider_base_url_v1,
        target_domain=(urlparse(site.base_url).hostname or ""),
        logs="",
        markdown_text="",
        csv_text="Categoria;Falha;Prova Técnica;Explicação;Prejuízo Estimado;Solução;Prioridade;Complexity\n",
    )
    db.session.add(audit)
    db.session.commit()

    try:
        enqueue_audit(audit.id)
    except Exception:
        audit.status = "error"
        audit.logs = (audit.logs or "") + "ERRO: falha ao enfileirar job (REDIS_URL/worker).\\n"
        db.session.commit()
    return redirect(url_for("audit.view_audit", audit_id=audit.id))


@bp.get("/run/<audit_id>")
@login_required
def view_audit(audit_id: str):
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    site = Site.query.filter_by(id=audit.site_id, org_id=current_user.org_id).first()
    return render_template("audit/view.html", audit=audit, site=site)


@bp.get("/run/<audit_id>/progress")
@login_required
def audit_progress(audit_id: str):
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    return jsonify(
        {
            "id": audit.id,
            "status": audit.status,
            "logs": audit.logs or "",
            "markdown_text": audit.markdown_text or "",
            "csv_text": audit.csv_text or "",
            "updated_utc": audit.updated_utc,
        }
    )


@bp.get("/run/<audit_id>/stream")
@login_required
def audit_stream(audit_id: str):
    """
    SSE stream (LOG/DATA/CSV_ROW) from DB tail.
    """
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()

    try:
        from_log = int(request.args.get("from_log") or "0")
        from_md = int(request.args.get("from_md") or "0")
        from_csv = int(request.args.get("from_csv") or "0")
    except Exception:
        from_log, from_md, from_csv = 0, 0, 0

    def emit(prefix: str, line: str) -> str:
        safe = (line or "").replace("\r", "").replace("\n", " ")
        return f"{prefix}:{safe}\n"

    def gen():
        log_pos = max(0, from_log)
        md_pos = max(0, from_md)
        csv_pos = max(0, from_csv)

        import time as _t

        try:
            while True:
                # keep DB session scoped to request context (stream_with_context wraps generator)
                db.session.expire_all()
                a = AuditRun.query.filter_by(id=audit.id).first()
                if not a:
                    break
                logs = a.logs or ""
                md = a.markdown_text or ""
                csv = a.csv_text or ""
                running = a.status in ("queued", "running")

                sent = False
                if log_pos < len(logs):
                    chunk = logs[log_pos:]
                    log_pos = len(logs)
                    for ln in chunk.splitlines():
                        yield emit("LOG", ln)
                    sent = True
                if md_pos < len(md):
                    chunk = md[md_pos:]
                    md_pos = len(md)
                    for ln in chunk.splitlines():
                        yield emit("DATA", ln)
                    sent = True
                if csv_pos < len(csv):
                    chunk = csv[csv_pos:]
                    csv_pos = len(csv)
                    for ln in chunk.splitlines():
                        yield emit("CSV_ROW", ln)
                    sent = True

                if not running and not sent:
                    break
                _t.sleep(0.25)
        finally:
            try:
                db.session.remove()
            except Exception:
                pass

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
