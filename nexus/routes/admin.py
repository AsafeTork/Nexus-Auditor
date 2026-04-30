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
from ..services.control_plane import build_agent_cards
from ..services.llm_providers import canonical_base_url_v1, list_provider_models, normalize_provider, validate_provider

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


def _master_email() -> str:
    return (os.getenv("MASTER_ADMIN_EMAIL", "") or "").strip().lower()


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

    # LLM sanity (provider-aware)
    org = Organization.query.filter_by(id=current_user.org_id).first() if getattr(current_user, "org_id", None) else None
    provider = normalize_provider(getattr(org, "llm_provider", "") if org else "", getattr(org, "llm_base_url_v1", "") if org else "")
    base_url = (
        (getattr(org, "llm_base_url_v1", "") if org else "")
        or current_app.config.get("LLM_BASE_URL_V1", "")
    )
    api_key = (
        (getattr(org, "llm_api_key", "") if org else "")
        or current_app.config.get("LLM_API_KEY", "")
    )
    model = (
        (getattr(org, "llm_model", "") if org else "")
        or current_app.config.get("LLM_DEFAULT_MODEL", "")
    )
    out["llm"] = {
        "provider": provider,
        "base_url_v1": canonical_base_url_v1(provider, base_url),
        "model": model,
        "api_key_mask": _mask(api_key, 6),
    }
    try:
        if not base_url or not model:
            raise RuntimeError("LLM provider/base_url/model not configured.")
        diag = validate_provider(
            provider=provider,
            base_url_v1=base_url,
            api_key=api_key,
            model=model,
            timeout_s=max(12, min(30, int(os.getenv("LLM_TIMEOUT_S", "20")))),
        )
        out["llm"]["ok"] = bool(diag.get("ok"))
        out["llm"]["sample"] = str(diag.get("sample") or "")[:120]
        out["llm"]["models_count"] = int(diag.get("models_count") or 0)
        out["llm"]["model_found"] = bool(diag.get("model_found")) if "model_found" in diag else None
        if not out["llm"]["ok"] and diag.get("error"):
            out["llm"]["error"] = str(diag.get("error"))
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
    org = Organization.query.filter_by(id=current_user.org_id).first()
    return render_template(
        "admin/home.html",
        diag=diag,
        audits=audits,
        sim=sim,
        llm_defaults={
            "provider": (getattr(org, "llm_provider", "openai_compatible") or "openai_compatible").strip(),
            "base_url_v1": (getattr(org, "llm_base_url_v1", "") or "").strip(),
            "api_key_mask": _mask((getattr(org, "llm_api_key", "") or "").strip(), 6),
            "model": (getattr(org, "llm_model", "") or "").strip(),
        },
    )


@bp.post("/admin/llm/save")
@login_required
@require_admin
def admin_llm_save():
    org = Organization.query.filter_by(id=current_user.org_id).first_or_404()
    provider = normalize_provider((request.form.get("provider") or "").strip(), (request.form.get("base_url_v1") or "").strip())
    base = canonical_base_url_v1(provider, (request.form.get("base_url_v1") or "").strip())
    api_key = (request.form.get("api_key") or "").strip()
    model = (request.form.get("model") or "").strip()
    org.llm_provider = provider
    org.llm_base_url_v1 = base
    if api_key:
        org.llm_api_key = api_key
    org.llm_model = model
    db.session.commit()
    flash("Configuração de IA salva para este org.", "ok")
    return redirect(url_for("admin.admin_home"))


@bp.get("/admin/diagnostics.json")
@login_required
@require_admin
def diagnostics_json():
    return jsonify(_diagnostics())


def _build_overview_rows(org_id: str):
    """
    Read-only overview data per site (no pipeline changes).
    """
    sites = Site.query.filter_by(org_id=org_id).order_by(Site.created_utc.desc()).limit(500).all()

    # Aggregate finding lifecycle counts per site (single query).
    # NOTE: use SQL text to avoid ImportError if monitoring models are not present in a given deployment.
    agg_map: dict[str, dict] = {}
    try:
        agg_sql = text(
            """
            SELECT
              site_id,
              SUM(CASE WHEN state = 'RESOLVED' THEN 1 ELSE 0 END) AS resolved_count,
              SUM(CASE WHEN state IN ('NEW','PERSISTING','REOPENED') THEN 1 ELSE 0 END) AS open_count,
              COALESCE(SUM(COALESCE(regression_count,0)), 0) AS regression_count,
              AVG(CASE WHEN resolution_time_s > 0 THEN resolution_time_s ELSE NULL END) AS avg_time_to_fix_s
            FROM monitoring_findings
            WHERE org_id = :org_id
            GROUP BY site_id
            """
        )
        rows = db.session.execute(agg_sql, {"org_id": org_id}).mappings().all()
        for r in rows:
            sid = str(r.get("site_id") or "")
            if not sid:
                continue
            agg_map[sid] = {
                "open": int(r.get("open_count") or 0),
                "resolved": int(r.get("resolved_count") or 0),
                "regressions": int(r.get("regression_count") or 0),
                "avg_time_s": int(r.get("avg_time_to_fix_s") or 0),
            }
    except Exception:
        # Table may not exist yet in some deployments; keep zeros.
        agg_map = {}

    rows = []
    for s in sites:
        a = agg_map.get(s.id, {"open": 0, "resolved": 0, "regressions": 0, "avg_time_s": 0})
        denom = max(1, int(a["open"]) + int(a["resolved"]))
        fix_success_rate = round((int(a["resolved"]) / denom) * 100.0, 2)

        last_run = {"id": "", "created_utc": "", "status": "", "decision_json": "", "verification_json": ""}
        try:
            last_sql = text(
                """
                SELECT id, created_utc, status, decision_json, verification_json
                FROM monitoring_runs
                WHERE org_id = :org_id AND site_id = :site_id
                ORDER BY created_utc DESC
                LIMIT 1
                """
            )
            rr = db.session.execute(last_sql, {"org_id": org_id, "site_id": s.id}).mappings().first()
            if rr:
                last_run = {
                    "id": str(rr.get("id") or ""),
                    "created_utc": str(rr.get("created_utc") or ""),
                    "status": str(rr.get("status") or ""),
                    "decision_json": str(rr.get("decision_json") or ""),
                    "verification_json": str(rr.get("verification_json") or ""),
                }
        except Exception:
            pass

        has_decision = bool((last_run.get("decision_json") or "").strip())
        has_verification = bool((last_run.get("verification_json") or "").strip())

        rows.append(
            {
                "site": {"id": s.id, "name": s.name, "base_url": s.base_url},
                "last_run": {
                    "id": last_run.get("id") or "",
                    "created_utc": last_run.get("created_utc") or "",
                    "status": last_run.get("status") or "",
                },
                "open_findings": int(a["open"]),
                "resolved_findings": int(a["resolved"]),
                "fix_success_rate": fix_success_rate,
                "avg_time_to_fix_s": int(a["avg_time_s"]),
                "regression_count": int(a["regressions"]),
                "has_decision_json": has_decision,
                "has_verification_json": has_verification,
            }
        )
    return rows


@bp.get("/admin/overview")
@login_required
@require_admin
def admin_overview():
    rows = _build_overview_rows(current_user.org_id)
    return render_template("admin/overview.html", rows=rows)


@bp.get("/admin/overview.json")
@login_required
@require_admin
def admin_overview_json():
    rows = _build_overview_rows(current_user.org_id)
    return jsonify({"ok": True, "rows": rows})


@bp.get("/admin/agent")
@login_required
@require_admin
def admin_agent():
    cards = build_agent_cards(current_user.org_id, limit=250)
    return render_template("admin/agent.html", cards=cards)


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
    title = f"[AUDIT] {audit.target_domain or 'audit'} · {audit.id}"
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
    # Window filter (days). Prevents "stale looking" console by default.
    days = int(os.getenv("ADMIN_LOGS_DAYS", "2") or "2")
    days = max(1, min(30, days))
    cutoff_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000
    cutoff_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff_ms / 1000))

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
            .filter(AuditEvent.ts_ms >= cutoff_ms)
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
        .filter(AuditEvent.ts_ms >= cutoff_ms)
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
            created_utc = (h.get(b"created_utc") or b"").decode("utf-8", "ignore")
            # Keep only recent runs for the console view
            if created_utc and created_utc < cutoff_utc:
                continue
            runs.append(
                {
                    "id": rid,
                    "status": (h.get(b"status") or b"").decode("utf-8", "ignore"),
                    "mode": (h.get(b"mode") or b"").decode("utf-8", "ignore"),
                    "created_utc": created_utc,
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
        logs_days=days,
        logs_cutoff_utc=cutoff_utc,
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

    # Counts / admin distribution
    total_users = len(users)
    admin_users_count = sum(1 for u in users if str(getattr(u, "role", "") or "").lower() == "admin")
    member_users_count = total_users - admin_users_count
    admin_count_by_org: dict[str, int] = {}
    for u in users:
        if str(getattr(u, "role", "") or "").lower() == "admin":
            admin_count_by_org[u.org_id] = admin_count_by_org.get(u.org_id, 0) + 1

    return render_template(
        "admin/users.html",
        master=global_view,
        master_email=_master_email(),
        orgs=orgs,
        users=users,
        org_map=org_map,
        sub_map=sub_map,
        plan_tiers=plan_tiers,
        sub_statuses=sub_statuses,
        roles=roles,
        total_users=total_users,
        admin_users_count=admin_users_count,
        member_users_count=member_users_count,
        admin_count_by_org=admin_count_by_org,
    )


@bp.post("/admin/user/create")
@login_required
@require_admin
def admin_user_create():
    """
    Create a new user.
    - If global view: can choose org_id or create a new org.
    - Else: creates inside current org only.
    """
    global_view = _is_master_admin() or _allow_global_admin_view()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    role = (request.form.get("role") or "member").strip().lower()
    org_id = (request.form.get("org_id") or "").strip()
    org_name = (request.form.get("org_name") or "").strip()

    if not email or "@" not in email:
        flash("Invalid email.", "error")
        return redirect(url_for("admin.admin_users"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin.admin_users"))
    if role not in ("admin", "member"):
        role = "member"

    if User.query.filter_by(email=email).first():
        flash("Email already registered.", "error")
        return redirect(url_for("admin.admin_users"))

    try:
        if not global_view:
            org_id = current_user.org_id
        else:
            # If master wants a brand new org, create it.
            if not org_id:
                if not org_name:
                    flash("Choose an organization or provide org_name.", "error")
                    return redirect(url_for("admin.admin_users"))
                org = Organization(name=org_name)
                db.session.add(org)
                db.session.flush()
                org_id = org.id
                # Ensure subscription exists
                db.session.add(Subscription(org_id=org_id, status="trialing", plan_tier="free"))
            else:
                # Ensure org exists
                Organization.query.filter_by(id=org_id).first_or_404()

        u = User(org_id=org_id, email=email, role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash("User created.", "ok")
    except Exception as e:
        db.session.rollback()
        flash(f"Create failed: {type(e).__name__}: {e}", "error")

    return redirect(url_for("admin.admin_users"))


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


@bp.route("/admin/llm/models.json", methods=["GET", "POST"])
@login_required
@require_admin
def admin_llm_models():
    org = Organization.query.filter_by(id=current_user.org_id).first()
    payload = request.get_json(silent=True) or {}
    provider = normalize_provider(
        payload.get("provider") or request.values.get("provider") or (getattr(org, "llm_provider", "") if org else ""),
        payload.get("base_url_v1") or request.values.get("base_url_v1") or (getattr(org, "llm_base_url_v1", "") if org else ""),
    )
    base_url = canonical_base_url_v1(
        provider,
        payload.get("base_url_v1") or request.values.get("base_url_v1") or (getattr(org, "llm_base_url_v1", "") if org else "") or current_app.config.get("LLM_BASE_URL_V1", ""),
    )
    api_key = (
        payload.get("api_key")
        or request.values.get("api_key")
        or (getattr(org, "llm_api_key", "") if org else "")
        or current_app.config.get("LLM_API_KEY", "")
    )
    force = str(payload.get("force") or request.values.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
    q = str(payload.get("q") or request.values.get("q") or "").strip().lower()

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
    cache_key = f"llm_models:v2:{provider}:{base_url}"
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
                    return jsonify({"ok": True, "provider": provider, "base_url_v1": base_url, "models": out, "cached": True})
        except Exception:
            pass

    try:
        models_api = list_provider_models(provider=provider, base_url_v1=base_url, api_key=api_key, timeout_s=12)
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
        return jsonify({"ok": True, "provider": provider, "base_url_v1": base_url, "models": out, "cached": False})
    except Exception as e:
        # If API fails, fallback to allowlist if present
        if allowlist:
            out = allowlist
            if q:
                out = [m for m in out if q in m.lower()]
            return jsonify({"ok": True, "provider": provider, "base_url_v1": base_url, "models": out, "cached": True, "fallback": "allowlist"})
        return jsonify({"ok": False, "provider": provider, "base_url_v1": base_url, "error": f"{type(e).__name__}: {e}", "models": []}), 400


@bp.post("/admin/llm/validate.json")
@login_required
@require_admin
def admin_llm_validate():
    org = Organization.query.filter_by(id=current_user.org_id).first()
    payload = request.get_json(silent=True) or {}
    provider = normalize_provider(
        payload.get("provider") or (getattr(org, "llm_provider", "") if org else ""),
        payload.get("base_url_v1") or (getattr(org, "llm_base_url_v1", "") if org else ""),
    )
    base_url = canonical_base_url_v1(
        provider,
        payload.get("base_url_v1") or (getattr(org, "llm_base_url_v1", "") if org else "") or current_app.config.get("LLM_BASE_URL_V1", ""),
    )
    api_key = (
        payload.get("api_key")
        or (getattr(org, "llm_api_key", "") if org else "")
        or current_app.config.get("LLM_API_KEY", "")
    )
    model = (payload.get("model") or (getattr(org, "llm_model", "") if org else "") or current_app.config.get("LLM_DEFAULT_MODEL", "")).strip()
    try:
        return jsonify(validate_provider(provider=provider, base_url_v1=base_url, api_key=api_key, model=model, timeout_s=15))
    except Exception as e:
        return jsonify({"ok": False, "provider": provider, "base_url_v1": base_url, "model": model, "error": f"{type(e).__name__}: {e}"}), 502


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
