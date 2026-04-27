from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import redis
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import text

from .. import db
from ..models import AuditEvent, AuditRun, Site, Subscription, is_org_admin
from ..security import require_admin

bp = Blueprint("admin", __name__)


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def _diagnostics() -> Dict[str, Any]:
    """
    Quick server-side health checks for admin panel.
    Avoid exposing secrets.
    """
    out: Dict[str, Any] = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # DB
    try:
        db.session.execute(text("SELECT 1"))
        out["db"] = {"ok": True}
    except Exception as e:
        out["db"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Redis
    try:
        rurl = current_app.config.get("REDIS_URL", "")
        conn = redis.from_url(rurl, socket_timeout=3, socket_connect_timeout=3)
        pong = conn.ping()
        out["redis"] = {"ok": bool(pong), "url": rurl.split("@")[-1] if rurl else ""}
    except Exception as e:
        out["redis"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # LLM sanity (non-stream short call)
    base_url = current_app.config.get("LLM_BASE_URL_V1", "")
    api_key = current_app.config.get("LLM_API_KEY", "")
    model = current_app.config.get("LLM_DEFAULT_MODEL", "")
    out["llm"] = {"base_url_v1": base_url, "model": model, "api_key_mask": _mask(api_key, 6)}
    try:
        if not base_url or not model:
            raise RuntimeError("LLM_BASE_URL_V1/LLM_DEFAULT_MODEL não configurados.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.1,
            "stream": False,
            "messages": [
                {"role": "system", "content": "Você é um verificador. Responda somente com a palavra OK."},
                {"role": "user", "content": "OK?"},
            ],
        }
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        j = r.json()
        content = str(((j.get("choices") or [None])[0] or {}).get("message", {}).get("content") or "")
        out["llm"]["ok"] = True
        out["llm"]["sample"] = content[:120]
    except Exception as e:
        out["llm"]["ok"] = False
        out["llm"]["error"] = f"{type(e).__name__}: {e}"

    return out


@bp.get("/admin")
@login_required
@require_admin
def admin_home():
    diag = _diagnostics()
    audits = AuditRun.query.filter_by(org_id=current_user.org_id).order_by(AuditRun.created_utc.desc()).limit(20).all()
    return render_template("admin/home.html", diag=diag, audits=audits)


@bp.get("/admin/diagnostics.json")
@login_required
@require_admin
def diagnostics_json():
    return jsonify(_diagnostics())


@bp.get("/admin/audits")
@login_required
@require_admin
def admin_audits():
    audits = AuditRun.query.filter_by(org_id=current_user.org_id).order_by(AuditRun.created_utc.desc()).limit(200).all()
    site_map = {s.id: s for s in Site.query.filter_by(org_id=current_user.org_id).all()}
    return render_template("admin/audits.html", audits=audits, site_map=site_map)


@bp.get("/admin/audit/<audit_id>")
@login_required
@require_admin
def admin_audit_detail(audit_id: str):
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    site = Site.query.filter_by(id=audit.site_id, org_id=current_user.org_id).first()
    events = (
        AuditEvent.query.filter_by(audit_run_id=audit.id)
        .order_by(AuditEvent.id.asc())
        .limit(5000)
        .all()
    )
    return render_template("admin/audit_detail.html", audit=audit, site=site, events=events)


@bp.post("/admin/audit/<audit_id>/delete")
@login_required
@require_admin
def admin_audit_delete(audit_id: str):
    """
    Delete an audit and its events (org-scoped).
    """
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    try:
        AuditEvent.query.filter_by(audit_run_id=audit.id).delete(synchronize_session=False)
        db.session.delete(audit)
        db.session.commit()
        flash("Auditoria excluída.", "ok")
    except Exception as e:
        db.session.rollback()
        flash(f"Falha ao excluir: {type(e).__name__}: {e}", "error")
    return redirect(url_for("admin.admin_audits"))
