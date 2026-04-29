from __future__ import annotations

import os
import json
import time
import uuid
from typing import Any, Dict

import redis
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy import text, func

from .. import db
from ..models import AuditEvent, AuditRun, Organization, Site, Subscription, User, is_org_admin
from ..security import require_admin
from ..services.queueing import enqueue_ui_lab
from ..services.github import create_issue
from ..services.audit_engine import list_models

bp = Blueprint("admin", __name__)


def _is_master_admin() -> bool:
    master = (os.getenv("MASTER_ADMIN_EMAIL", "") or "").strip().lower()
    return bool(master) and (str(getattr(current_user, "email", "") or "").strip().lower() == master)


def _allow_global_admin_view() -> bool:
    """
    If enabled, any admin can manage all users/orgs (not only MASTER_ADMIN_EMAIL).
    Default: off.
    """
    return str(os.getenv("ADMIN_GLOBAL_USERS", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


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

    # LLM sanity (non-stream short call with retries)
    base_url = current_app.config.get("LLM_BASE_URL_V1", "")
    api_key = current_app.config.get("LLM_API_KEY", "")
    model = current_app.config.get("LLM_DEFAULT_MODEL", "")
    out["llm"] = {"base_url_v1": base_url, "model": model, "api_key_mask": _mask(api_key, 6)}
    try:
        if not base_url or not model:
            raise RuntimeError("LLM_BASE_URL_V1/LLM_DEFAULT_MODEL not configured.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.1,
            "stream": False,
            "messages": [
                {"role": "system", "content": "You are a health check. Reply with OK only."},
                {"role": "user", "content": "OK?"},
            ],
        }
        retry_statuses = {429, 502, 503, 504, 520, 524}
        timeout_s = int(os.getenv("LLM_TIMEOUT_S", "20"))
        timeout_s = max(20, min(120, timeout_s))
        backoffs = [1.0, 2.0, 4.0]
        last_status = None
        last_text = ""
        r = None
        for attempt in range(3):
            rr = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
            last_status = rr.status_code
            try:
                last_text = (rr.text or "")[:240]
            except Exception:
                last_text = ""
            if rr.status_code in retry_statuses and attempt < 2:
                try:
                    rr.close()
                except Exception:
                    pass
                time.sleep(backoffs[attempt])
                continue
            rr.raise_for_status()
            r = rr
            break
        if r is None:
            raise RuntimeError(f"LLM request failed (status={last_status})")
        j = r.json()
        content = str(((j.get("choices") or [None])[0] or {}).get("message", {}).get("content") or "")
        out["llm"]["ok"] = True
        out["llm"]["sample"] = content[:120]
    except Exception as e:
        out["llm"]["ok"] = False
        out["llm"]["error"] = f"{type(e).__name__}: {e}"
        # Useful snippet for debugging upstream gateways (best-effort)
        try:
            if "last_status" in locals() and last_status is not None:
                out["llm"]["status"] = int(last_status)
            if "last_text" in locals() and last_text:
                out["llm"]["body_head"] = last_text
        except Exception:
            pass

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
    org = Organization.query.filter_by(id=current_user.org_id).first()
    return render_template(
        "admin/home.html",
        diag=diag,
        audits=audits,
        sim=sim,
        llm_defaults={
            "base_url_v1": (getattr(org, "llm_base_url_v1", "") or "").strip(),
            "model": (getattr(org, "llm_model", "") or "").strip(),
        },
    )


@bp.post("/admin/llm/save")
@login_required
@require_admin
def admin_llm_save():
    org = Organization.query.filter_by(id=current_user.org_id).first_or_404()
    base = (request.form.get("base_url_v1") or "").strip()
    model = (request.form.get("model") or "").strip()
    org.llm_base_url_v1 = base
    org.llm_model = model
    db.session.commit()
    flash("Configuração de IA salva para este org.", "ok")
    return redirect(url_for("admin.admin_home"))


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
    # Limits (big by default, but bounded)
    audits_limit = int(os.getenv("ADMIN_LOGS_AUDITS_LIMIT", "300"))
    events_limit = int(os.getenv("ADMIN_LOGS_EVENTS_LIMIT", "2500"))
    err_limit = int(os.getenv("ADMIN_LOGS_ERROR_EVENTS_LIMIT", "5000"))
    runs_limit = int(os.getenv("ADMIN_LOGS_RUNS_LIMIT", "50"))

    # Recent audits (org scope)
    audits = (
        AuditRun.query.filter_by(org_id=current_user.org_id)
        .order_by(AuditRun.created_utc.desc())
        .limit(audits_limit)
        .all()
    )

    # Recent events (fast path using recent audits)
    audit_ids = [a.id for a in audits]
    events = []
    if audit_ids:
        events = (
            AuditEvent.query.filter(AuditEvent.audit_run_id.in_(audit_ids))
            .order_by(AuditEvent.id.desc())
            .limit(events_limit)
            .all()
        )

    # Error events across the whole org (JOIN avoids missing older errors)
    # Some providers/loggers may write errors as INFO with "error/erro/exception" in message,
    # so we also match by message keywords.
    err_levels = ["ERROR", "ERR", "WARN", "WARNING", "CRITICAL"]
    msg = func.lower(AuditEvent.message)
    msg_hit = (
        msg.like("%error%")
        | msg.like("%erro%")
        | msg.like("%exception%")
        | msg.like("%traceback%")
        | msg.like("%forbidden%")
        | msg.like("%timeout%")
        | msg.like("%failed%")
        | msg.like("%falha%")
    )
    error_rows = (
        db.session.query(AuditEvent, AuditRun)
        .join(AuditRun, AuditRun.id == AuditEvent.audit_run_id)
        .filter(AuditRun.org_id == current_user.org_id)
        .filter(func.upper(AuditEvent.level).in_(err_levels) | msg_hit)
        .order_by(AuditEvent.id.desc())
        .limit(err_limit)
        .all()
    )
    error_events = [
        {
            "ts_ms": e.ts_ms,
            "level": e.level,
            "layer": e.layer,
            "audit_run_id": e.audit_run_id,
            "target_domain": (a.target_domain or a.id),
            "message": e.message,
        }
        for (e, a) in error_rows
    ]

    # Failed audits list (most important at the top)
    failed_audits = (
        AuditRun.query.filter_by(org_id=current_user.org_id, status="error")
        .order_by(AuditRun.created_utc.desc())
        .limit(200)
        .all()
    )
    # UI/Backend lab runs from redis
    conn = _redis_conn()
    runs = []
    try:
        ids = conn.lrange(_ui_index_key(current_user.org_id), 0, runs_limit)
        for rid in ids:
            rid = rid.decode("utf-8") if isinstance(rid, (bytes, bytearray)) else str(rid)
            h = conn.hgetall(_ui_key(current_user.org_id, rid)) or {}
            err = (h.get(b"error") or b"").decode("utf-8", "ignore")
            if err:
                err = err.replace("\\n", "\n")
            runs.append(
                {
                    "id": rid,
                    "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
                    "mode": (h.get(b"mode") or b"").decode("utf-8", "ignore"),
                    "created_utc": (h.get(b"created_utc") or b"").decode("utf-8", "ignore"),
                    "error": err,
                }
            )
    except Exception:
        runs = []

    # Tail of logs for failed UI/Backend runs (so errors don't "disappear")
    tail_chars = int(os.getenv("ADMIN_LOGS_RUN_TAIL_CHARS", "6000"))
    max_error_tails = int(os.getenv("ADMIN_LOGS_MAX_ERROR_RUN_TAILS", "12"))
    run_error_tails = []
    try:
        count = 0
        for r in runs:
            if count >= max_error_tails:
                break
            if str(r.get("status")) != "error":
                continue
            rid = str(r.get("id") or "")
            if not rid:
                continue
            key = _ui_key(current_user.org_id, rid)
            raw = conn.get(key + ":logs") or b""
            txt = raw.decode("utf-8", "ignore")
            if txt:
                txt = txt.replace("\\n", "\n")
            if tail_chars > 0 and len(txt) > tail_chars:
                txt = txt[-tail_chars:]
            run_error_tails.append(
                {
                    "id": rid,
                    "mode": r.get("mode") or "",
                    "created_utc": r.get("created_utc") or "",
                    "error": r.get("error") or "",
                    "tail": txt,
                }
            )
            count += 1
    except Exception:
        run_error_tails = []
    # id->domain map
    audit_map = {a.id: a for a in audits}
    return render_template(
        "admin/logs.html",
        diag=diag,
        events=events,
        error_events=error_events,
        failed_audits=failed_audits,
        audits=audits,
        audit_map=audit_map,
        ui_runs=runs,
        run_error_tails=run_error_tails,
    )


@bp.get("/admin/users")
@login_required
@require_admin
def admin_users():
    """
    Admin user/org management.
    - Master admin (MASTER_ADMIN_EMAIL) can see all orgs/users.
    - Regular org admins see only their org.
    """
    master = _is_master_admin()
    global_view = master or _allow_global_admin_view()
    if global_view:
        orgs = Organization.query.order_by(Organization.created_utc.desc()).limit(500).all()
        users = User.query.order_by(User.created_utc.desc()).limit(2000).all()
        subs = Subscription.query.order_by(Subscription.created_utc.desc()).limit(1000).all()
    else:
        orgs = Organization.query.filter_by(id=current_user.org_id).all()
        users = User.query.filter_by(org_id=current_user.org_id).order_by(User.created_utc.desc()).limit(500).all()
        subs = Subscription.query.filter_by(org_id=current_user.org_id).limit(5).all()

    org_map = {o.id: o for o in orgs}
    sub_map = {s.org_id: s for s in subs}

    plan_tiers = ["free", "pro", "enterprise"]
    sub_statuses = ["inactive", "trialing", "active", "past_due", "canceled"]
    roles = ["member", "admin"]

    return render_template(
        "admin/users.html",
        master=global_view,
        orgs=orgs,
        users=users,
        org_map=org_map,
        sub_map=sub_map,
        plan_tiers=plan_tiers,
        sub_statuses=sub_statuses,
        roles=roles,
    )


@bp.post("/admin/user/<user_id>/role")
@login_required
@require_admin
def admin_user_set_role(user_id: str):
    master = _is_master_admin() or _allow_global_admin_view()
    u = User.query.filter_by(id=user_id).first_or_404()
    if (not master) and u.org_id != current_user.org_id:
        flash("Forbidden.", "error")
        return redirect(url_for("admin.admin_users"))

    role = (request.form.get("role") or "").strip().lower()
    if role not in ("admin", "member"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin.admin_users"))

    # Prevent removing the last admin of an org
    if u.role == "admin" and role != "admin":
        admins = User.query.filter_by(org_id=u.org_id, role="admin").count()
        if admins <= 1:
            flash("You cannot remove the last admin of an organization.", "error")
            return redirect(url_for("admin.admin_users"))

    u.role = role
    db.session.commit()
    flash("User updated.", "ok")
    return redirect(url_for("admin.admin_users"))


@bp.post("/admin/user/<user_id>/delete")
@login_required
@require_admin
def admin_user_delete(user_id: str):
    master = _is_master_admin() or _allow_global_admin_view()
    u = User.query.filter_by(id=user_id).first_or_404()
    if (not master) and u.org_id != current_user.org_id:
        flash("Forbidden.", "error")
        return redirect(url_for("admin.admin_users"))

    # Prevent deleting yourself
    if str(getattr(current_user, "id", "")) == str(u.id):
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.admin_users"))

    # Prevent deleting the last admin of an org
    if str(u.role or "").lower() == "admin":
        admins = User.query.filter_by(org_id=u.org_id, role="admin").count()
        if admins <= 1:
            flash("You cannot delete the last admin of an organization.", "error")
            return redirect(url_for("admin.admin_users"))

    try:
        db.session.delete(u)
        db.session.commit()
        flash("User deleted.", "ok")
    except Exception as e:
        db.session.rollback()
        flash(f"Delete failed: {type(e).__name__}: {e}", "error")
    return redirect(url_for("admin.admin_users"))


@bp.post("/admin/org/<org_id>/subscription")
@login_required
@require_admin
def admin_org_set_subscription(org_id: str):
    master = _is_master_admin() or _allow_global_admin_view()
    if (not master) and org_id != current_user.org_id:
        flash("Forbidden.", "error")
        return redirect(url_for("admin.admin_users"))

    org = Organization.query.filter_by(id=org_id).first_or_404()
    sub = Subscription.query.filter_by(org_id=org.id).first()
    if not sub:
        sub = Subscription(org_id=org.id, status="trialing", plan_tier="free")
        db.session.add(sub)

    status = (request.form.get("status") or "").strip().lower()
    plan_tier = (request.form.get("plan_tier") or "").strip().lower()

    if status and status not in ("inactive", "trialing", "active", "past_due", "canceled"):
        flash("Invalid subscription status.", "error")
        return redirect(url_for("admin.admin_users"))
    if plan_tier and plan_tier not in ("free", "pro", "enterprise"):
        flash("Invalid plan tier.", "error")
        return redirect(url_for("admin.admin_users"))

    if status:
        sub.status = status
    if plan_tier:
        sub.plan_tier = plan_tier
    sub.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.session.commit()
    flash("Subscription updated.", "ok")
    return redirect(url_for("admin.admin_users"))


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


@bp.get("/admin/backend-lab")
@login_required
@require_admin
def backend_lab():
    """
    Backend Lab: generates a single prompt for SOLO to improve backend code.
    Uses the same run storage as UI Lab.
    """
    org_id = current_user.org_id
    conn = _redis_conn()
    run_id = (request.args.get("run") or "").strip()
    runs = []
    try:
        ids = conn.lrange(_ui_index_key(org_id), 0, 20)
        for rid in ids:
            rid = rid.decode("utf-8") if isinstance(rid, (bytes, bytearray)) else str(rid)
            h = conn.hgetall(_ui_key(org_id, rid)) or {}
            mode = (h.get(b"mode") or b"").decode("utf-8", "ignore")
            if mode != "backend":
                continue
            runs.append(
                {
                    "id": rid,
                    "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
                    "mode": mode,
                    "created_utc": (h.get(b"created_utc") or b"").decode("utf-8", "ignore"),
                }
            )
    except Exception:
        runs = []
    return render_template("admin/backend_lab.html", run_id=run_id, runs=runs)


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
    force = (request.args.get("force") or "").strip() in ("1", "true", "yes", "on")
    q = (request.args.get("q") or "").strip().lower()

    # Optional allowlist / custom list
    raw_allow = (os.getenv("LLM_MODELS_ALLOWLIST", "") or "").strip()
    allowlist = []
    if raw_allow:
        # Accept CSV or newline-separated
        parts = [p.strip() for p in raw_allow.replace("\n", ",").split(",")]
        allowlist = [p for p in parts if p]

    # Cache in Redis (best-effort)
    cache_ttl = int(os.getenv("LLM_MODELS_CACHE_TTL_S", "60") or "60")
    cache_ttl = max(10, min(600, cache_ttl))
    cache_key = f"llm_models:v1:{base_url}"
    conn = _redis_conn()
    if conn and (not force):
        try:
            cached = conn.get(cache_key)
            if cached:
                models = json.loads(cached)
                if isinstance(models, list):
                    out = [str(m) for m in models]
                    if q:
                        out = [m for m in out if q in m.lower()]
                    return jsonify({"ok": True, "models": out, "cached": True})
        except Exception:
            pass

    try:
        models_api = list_models(base_url_v1=base_url, api_key=api_key, timeout_s=12)
        # Merge allowlist first, then API models (unique, stable order)
        seen = set()
        merged = []
        for m in (allowlist or []) + (models_api or []):
            mm = str(m or "").strip()
            if not mm or mm in seen:
                continue
            seen.add(mm)
            merged.append(mm)
        if conn:
            try:
                conn.set(cache_key, json.dumps(merged, ensure_ascii=False), ex=cache_ttl)
            except Exception:
                pass
        out = merged
        if q:
            out = [m for m in out if q in m.lower()]
        return jsonify({"ok": True, "models": out, "cached": False})
    except Exception as e:
        # If API fails, fallback to allowlist if present
        if allowlist:
            out = allowlist
            if q:
                out = [m for m in out if q in m.lower()]
            return jsonify({"ok": True, "models": out, "cached": True, "fallback": "allowlist"})
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}), 400


@bp.post("/admin/ui-lab/run")
@login_required
@require_admin
def ui_lab_run():
    """
    UI Lab v2:
    - No URL/domain input (always analyzes the whole UI surface)
    - No screenshot upload (auto uses templates as source of truth)
    - Output is a single PROMPT for SOLO to execute (not a long report)
    """
    goal = (request.form.get("goal") or "").strip() or "Deixar a UI mais premium, clara e consistente."
    run_id = str(uuid.uuid4())
    conn = _redis_conn()
    conn.hset(
        _ui_key(current_user.org_id, run_id),
        mapping={
            "status": "queued",
            "mode": "auto",
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    conn.delete(_ui_key(current_user.org_id, run_id) + ":logs")
    conn.delete(_ui_key(current_user.org_id, run_id) + ":result")
    conn.rpush(_ui_index_key(current_user.org_id), run_id)
    enqueue_ui_lab(run_id, current_user.org_id, "auto", {"goal": goal})
    flash("UI Lab enfileirado. Ele vai analisar TODA a UI e gerar um PROMPT pronto para copiar/colar.", "ok")
    return redirect(url_for("admin.ui_lab", run=run_id))


@bp.post("/admin/backend-lab/run")
@login_required
@require_admin
def backend_lab_run():
    goal = (request.form.get("goal") or "").strip() or "Melhorar robustez, segurança e performance do backend."
    run_id = str(uuid.uuid4())
    conn = _redis_conn()
    conn.hset(
        _ui_key(current_user.org_id, run_id),
        mapping={
            "status": "queued",
            "mode": "backend",
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    conn.delete(_ui_key(current_user.org_id, run_id) + ":logs")
    conn.delete(_ui_key(current_user.org_id, run_id) + ":result")
    conn.rpush(_ui_index_key(current_user.org_id), run_id)
    enqueue_ui_lab(run_id, current_user.org_id, "backend", {"goal": goal})
    flash("Backend Lab enfileirado. Ele vai gerar um PROMPT pronto para copiar/colar.", "ok")
    return redirect(url_for("admin.backend_lab", run=run_id))


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
