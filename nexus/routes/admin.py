from __future__ import annotations

import os
import time
from typing import Any, Dict

import redis
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy import text

from .. import db
from ..models import AuditEvent, AuditRun, Site, Subscription, is_org_admin
from ..security import require_admin
from ..services.audit_engine import call_llm_non_stream
from ..services.ui_review import read_text_files, summarize_screenshot
from ..services.github import create_issue

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


def _run_ui_review(user_goal: str, context: str) -> str:
    base_url = current_app.config.get("LLM_BASE_URL_V1", "")
    api_key = current_app.config.get("LLM_API_KEY", "")
    model = current_app.config.get("LLM_DEFAULT_MODEL", "") or "deepseek-chat"

    system = (
        "Você é um especialista em UX/UI e Frontend. Analise a UI descrita e produza melhorias práticas, "
        "priorizando layout, hierarquia visual, espaçamento, responsividade (mobile+desktop), tipografia e acessibilidade. "
        "NÃO invente páginas. Se algo estiver ausente, indique como coletar a informação. "
        "Formato de saída: Markdown com seções: 1) Diagnóstico rápido 2) Problemas (com impacto) 3) Propostas de layout "
        "4) Ajustes mobile 5) Ajustes desktop 6) Paleta/contraste 7) Lista de mudanças em arquivos (path + o que mudar)."
    )

    user = f"Objetivo do admin: {user_goal}\n\nContexto/UI:\n{context}\n"
    return call_llm_non_stream(
        base_url_v1=base_url,
        api_key=api_key,
        model=model,
        temperature=0.2,
        system_prompt=system,
        user_prompt=user,
        timeout_s=240,
    )


@bp.get("/admin/ui-lab")
@login_required
@require_admin
def ui_lab():
    last = session.get("ui_lab_last_md", "")
    last_mode = session.get("ui_lab_last_mode", "")
    return render_template("admin/ui_lab.html", last_md=last, last_mode=last_mode)


@bp.post("/admin/ui-lab/templates")
@login_required
@require_admin
def ui_lab_templates():
    goal = (request.form.get("goal") or "").strip() or "Melhorar a UI para ficar moderna, limpa e premium."
    # Analyze key templates
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    tpl_dir = os.path.join(root, "templates")
    targets = [
        os.path.join(tpl_dir, "layout.html"),
        os.path.join(tpl_dir, "dashboard", "home.html"),
        os.path.join(tpl_dir, "audit", "view.html"),
        os.path.join(tpl_dir, "admin", "home.html"),
        os.path.join(tpl_dir, "admin", "audits.html"),
        os.path.join(tpl_dir, "admin", "audit_detail.html"),
        os.path.join(tpl_dir, "auth", "login.html"),
        os.path.join(tpl_dir, "auth", "register.html"),
    ]
    context = read_text_files([p for p in targets if os.path.exists(p)], max_chars_each=12000)
    md = _run_ui_review(goal, context)
    session["ui_lab_last_md"] = md
    session["ui_lab_last_mode"] = "templates"
    flash("Análise por templates concluída.", "ok")
    return redirect(url_for("admin.ui_lab"))


@bp.post("/admin/ui-lab/url")
@login_required
@require_admin
def ui_lab_url():
    import requests

    goal = (request.form.get("goal") or "").strip() or "Avaliar e sugerir melhorias de layout e responsividade."
    url = (request.form.get("url") or "").strip()
    if not url:
        flash("Informe uma URL.", "error")
        return redirect(url_for("admin.ui_lab"))
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "NexusAuditor/1.0"})
        r.raise_for_status()
        html = r.text
        if len(html) > 20000:
            html = html[:20000] + "\n<!-- ... truncado ... -->"
        context = f"URL: {url}\n\nHTML:\n{html}"
        md = _run_ui_review(goal, context)
        session["ui_lab_last_md"] = md
        session["ui_lab_last_mode"] = "url"
        flash("Análise por URL concluída.", "ok")
    except Exception as e:
        flash(f"Falha ao buscar URL: {type(e).__name__}: {e}", "error")
    return redirect(url_for("admin.ui_lab"))


@bp.post("/admin/ui-lab/screenshot")
@login_required
@require_admin
def ui_lab_screenshot():
    goal = (request.form.get("goal") or "").strip() or "Sugerir melhorias de layout e hierarquia visual."
    notes = (request.form.get("notes") or "").strip()
    f = request.files.get("screenshot")
    if not f:
        flash("Envie um screenshot (PNG/JPG).", "error")
        return redirect(url_for("admin.ui_lab"))
    try:
        data = f.read()
        meta = summarize_screenshot(data)
        context = (
            f"Screenshot meta: {meta.width}x{meta.height}, bytes={meta.size_bytes}, cores={meta.dominant_hex}\n"
            f"Observações do admin: {notes}\n"
            "Atenção: você NÃO está vendo a imagem; baseie-se em heurísticas + notas.\n"
        )
        md = _run_ui_review(goal, context)
        session["ui_lab_last_md"] = md
        session["ui_lab_last_mode"] = "screenshot"
        flash("Análise por screenshot concluída (via metadados + notas).", "ok")
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
