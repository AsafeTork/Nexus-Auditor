from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict

import redis
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy import text

from .. import db
from ..models import AuditEvent, AuditRun, Site, Subscription, is_org_admin
from ..security import require_admin
from ..services.queueing import enqueue_ui_lab
from ..services.ui_review import summarize_screenshot
from ..services.github import create_issue
from ..services.audit_engine import list_models

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
    sim = {
        "role": session.get("sim_role") or "",
        "sub_status": session.get("sim_sub_status") or "",
    }
    return render_template("admin/home.html", diag=diag, audits=audits, sim=sim)


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


@bp.post("/admin/audit/<audit_id>/publish_github")
@login_required
@require_admin
def admin_audit_publish_github(audit_id: str):
    audit = AuditRun.query.filter_by(id=audit_id, org_id=current_user.org_id).first_or_404()
    title = f"[Nexus Auditor] {audit.target_domain or 'audit'} · {audit.id}"
    body = (
        f"## Relatório de Auditoria\n\n"
        f"**Audit ID:** `{audit.id}`\n"
        f"**Status:** `{audit.status}`\n"
        f"**Modelo:** `{audit.model}`\n"
        f"**Gerado em:** `{audit.created_utc}`\n\n"
        f"---\n\n"
        f"{audit.markdown_text or '(sem markdown)'}\n\n"
        f"---\n\n"
        f"## Matriz CSV\n\n"
        f"```csv\n{audit.csv_text or ''}\n```\n"
    )
    try:
        url = create_issue(title=title, body_md=body, labels=["audit"])
        flash(f"Publicado no GitHub: {url}", "ok")
    except Exception as e:
        flash(f"Falha ao publicar no GitHub: {type(e).__name__}: {e}", "error")
    return redirect(url_for("admin.admin_audits"))


@bp.get("/admin/logs")
@login_required
@require_admin
def admin_logs():
    """
    Admin log geral: eventos de auditoria + execuções do UI Lab + diagnóstico.
    """
    diag = _diagnostics()
    # Recent audit events (scoped by org via recent audit ids)
    audits = (
        AuditRun.query.filter_by(org_id=current_user.org_id)
        .order_by(AuditRun.created_utc.desc())
        .limit(40)
        .all()
    )
    audit_ids = [a.id for a in audits]
    events = []
    if audit_ids:
        events = (
            AuditEvent.query.filter(AuditEvent.audit_run_id.in_(audit_ids))
            .order_by(AuditEvent.id.desc())
            .limit(800)
            .all()
        )
    # UI lab runs from redis
    conn = _redis_conn()
    runs = []
    try:
        ids = conn.lrange(_ui_index_key(current_user.org_id), 0, 10)
        for rid in ids:
            rid = rid.decode("utf-8") if isinstance(rid, (bytes, bytearray)) else str(rid)
            h = conn.hgetall(_ui_key(current_user.org_id, rid)) or {}
            runs.append(
                {
                    "id": rid,
                    "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
                    "mode": (h.get(b"mode") or b"").decode("utf-8", "ignore"),
                    "created_utc": (h.get(b"created_utc") or b"").decode("utf-8", "ignore"),
                }
            )
    except Exception:
        runs = []
    # id->domain map
    audit_map = {a.id: a for a in audits}
    return render_template("admin/logs.html", diag=diag, events=events, audits=audits, audit_map=audit_map, ui_runs=runs)


def _redis_conn():
    return redis.from_url(current_app.config.get("REDIS_URL", "redis://localhost:6379/0"))


def _ui_key(org_id: str, run_id: str) -> str:
    return f"ui_lab:{org_id}:{run_id}"


def _ui_index_key(org_id: str) -> str:
    return f"ui_lab:index:{org_id}"


@bp.get("/admin/ui-lab")
@login_required
@require_admin
def ui_lab():
    org_id = current_user.org_id
    conn = _redis_conn()
    run_id = (request.args.get("run") or "").strip()
    # last runs
    runs = []
    try:
        ids = conn.lrange(_ui_index_key(org_id), 0, 10)
        for rid in ids:
            rid = rid.decode("utf-8") if isinstance(rid, (bytes, bytearray)) else str(rid)
            h = conn.hgetall(_ui_key(org_id, rid))
            runs.append(
                {
                    "id": rid,
                    "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
                    "mode": (h.get(b"mode") or b"").decode("utf-8", "ignore"),
                    "created_utc": (h.get(b"created_utc") or b"").decode("utf-8", "ignore"),
                }
            )
    except Exception:
        runs = []
    return render_template("admin/ui_lab.html", run_id=run_id, runs=runs)


@bp.get("/admin/ui-lab/run/<run_id>.json")
@login_required
@require_admin
def ui_lab_run_json(run_id: str):
    org_id = current_user.org_id
    conn = _redis_conn()
    key = _ui_key(org_id, run_id)
    h = conn.hgetall(key) or {}
    logs = conn.get(key + ":logs") or b""
    result = conn.get(key + ":result") or b""
    return jsonify(
        {
            "id": run_id,
            "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
            "mode": (h.get(b"mode") or b"").decode("utf-8", "ignore"),
            "created_utc": (h.get(b"created_utc") or b"").decode("utf-8", "ignore"),
            "updated_utc": (h.get(b"updated_utc") or b"").decode("utf-8", "ignore"),
            "error": (h.get(b"error") or b"").decode("utf-8", "ignore"),
            "logs": logs.decode("utf-8", "ignore"),
            "result_md": result.decode("utf-8", "ignore"),
        }
    )


@bp.get("/admin/llm/models.json")
@login_required
@require_admin
def admin_llm_models():
    base_url = (request.args.get("base_url_v1") or "").strip() or current_app.config.get("LLM_BASE_URL_V1", "")
    api_key = current_app.config.get("LLM_API_KEY", "")
    try:
        models = list_models(base_url_v1=base_url, api_key=api_key, timeout_s=12)
        return jsonify({"ok": True, "models": models})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}), 400


@bp.post("/admin/ui-lab/templates")
@login_required
@require_admin
def ui_lab_templates():
    goal = (request.form.get("goal") or "").strip() or "Melhorar a UI para ficar moderna, limpa e premium."
    base_url_v1 = (request.form.get("base_url_v1") or "").strip()
    model = (request.form.get("model") or "").strip()
    run_id = str(uuid.uuid4())
    conn = _redis_conn()
    conn.hset(
        _ui_key(current_user.org_id, run_id),
        mapping={
            "status": "queued",
            "mode": "templates",
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    conn.delete(_ui_key(current_user.org_id, run_id) + ":logs")
    conn.delete(_ui_key(current_user.org_id, run_id) + ":result")
    conn.rpush(_ui_index_key(current_user.org_id), run_id)
    payload = {"goal": goal}
    if base_url_v1:
        payload["base_url_v1"] = base_url_v1
    if model:
        payload["model"] = model
    enqueue_ui_lab(run_id, current_user.org_id, "templates", payload)
    flash("UI Lab enfileirado (templates). Veja logs/resultado abaixo.", "ok")
    return redirect(url_for("admin.ui_lab", run=run_id))


@bp.post("/admin/ui-lab/url")
@login_required
@require_admin
def ui_lab_url():
    goal = (request.form.get("goal") or "").strip() or "Avaliar e sugerir melhorias de layout e responsividade."
    url = (request.form.get("url") or "").strip()
    base_url_v1 = (request.form.get("base_url_v1") or "").strip()
    model = (request.form.get("model") or "").strip()
    if not url:
        flash("Informe uma URL.", "error")
        return redirect(url_for("admin.ui_lab"))
    run_id = str(uuid.uuid4())
    conn = _redis_conn()
    conn.hset(
        _ui_key(current_user.org_id, run_id),
        mapping={
            "status": "queued",
            "mode": "url",
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    conn.delete(_ui_key(current_user.org_id, run_id) + ":logs")
    conn.delete(_ui_key(current_user.org_id, run_id) + ":result")
    conn.rpush(_ui_index_key(current_user.org_id), run_id)
    payload = {"goal": goal, "url": url}
    if base_url_v1:
        payload["base_url_v1"] = base_url_v1
    if model:
        payload["model"] = model
    enqueue_ui_lab(run_id, current_user.org_id, "url", payload)
    flash("UI Lab enfileirado (URL). Veja logs/resultado abaixo.", "ok")
    return redirect(url_for("admin.ui_lab", run=run_id))


@bp.post("/admin/ui-lab/screenshot")
@login_required
@require_admin
def ui_lab_screenshot():
    goal = (request.form.get("goal") or "").strip() or "Sugerir melhorias de layout e hierarquia visual."
    notes = (request.form.get("notes") or "").strip()
    base_url_v1 = (request.form.get("base_url_v1") or "").strip()
    model = (request.form.get("model") or "").strip()
    f = request.files.get("screenshot")
    if not f:
        flash("Envie um screenshot (PNG/JPG).", "error")
        return redirect(url_for("admin.ui_lab"))
    try:
        data = f.read()
        meta = summarize_screenshot(data)
        run_id = str(uuid.uuid4())
        conn = _redis_conn()
        conn.hset(
            _ui_key(current_user.org_id, run_id),
            mapping={
                "status": "queued",
                "mode": "screenshot",
                "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        conn.delete(_ui_key(current_user.org_id, run_id) + ":logs")
        conn.delete(_ui_key(current_user.org_id, run_id) + ":result")
        conn.rpush(_ui_index_key(current_user.org_id), run_id)
        enqueue_ui_lab(
            run_id,
            current_user.org_id,
            "screenshot",
            {
                "goal": goal,
                "notes": notes,
                "meta": {
                    "width": meta.width,
                    "height": meta.height,
                    "size_bytes": meta.size_bytes,
                    "dominant_hex": meta.dominant_hex,
                },
                "base_url_v1": base_url_v1,
                "model": model,
            },
        )
        flash("UI Lab enfileirado (screenshot). Veja logs/resultado abaixo.", "ok")
        return redirect(url_for("admin.ui_lab", run=run_id))
    except Exception as e:
        flash(f"Falha ao processar screenshot: {type(e).__name__}: {e}", "error")
    return redirect(url_for("admin.ui_lab"))


@bp.post("/admin/sim")
@login_required
@require_admin
def admin_set_sim():
    """
    Simulation mode for admin: simulate subscription status and role
    for UI/flows (does NOT change DB).
    """
    role = (request.form.get("role") or "").strip().lower()
    sub_status = (request.form.get("sub_status") or "").strip().lower()

    if role not in ("", "member", "admin"):
        role = ""
    if sub_status not in ("", "inactive", "trialing", "active", "past_due", "canceled"):
        sub_status = ""

    if role:
        session["sim_role"] = role
    else:
        session.pop("sim_role", None)

    if sub_status:
        session["sim_sub_status"] = sub_status
    else:
        session.pop("sim_sub_status", None)

    flash("Simulação atualizada.", "ok")
    return redirect(url_for("admin.admin_home"))


@bp.post("/admin/sim/clear")
@login_required
@require_admin
def admin_clear_sim():
    session.pop("sim_role", None)
    session.pop("sim_sub_status", None)
    flash("Simulação desativada.", "ok")
    return redirect(url_for("admin.admin_home"))
