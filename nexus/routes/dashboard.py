from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from flask import Blueprint, render_template, session
from flask_login import login_required, current_user

from .. import db
from ..models import Organization, Site, AuditRun, Subscription
from ..services.cache import cache_get_json, cache_set_json
from ..services.control_plane import build_agent_cards

bp = Blueprint("dashboard", __name__)


def _status_rank(status: str) -> int:
    return {
        "CRITICAL": 3,
        "AT_RISK": 2,
        "PROTECTED": 1,
        "NO_DATA": 0,
    }.get(str(status or "").upper(), 0)


def _site_risk_score(card: dict) -> int:
    status = str(((card or {}).get("overall") or {}).get("status") or "").upper()
    base = {
        "CRITICAL": 92,
        "AT_RISK": 68,
        "PROTECTED": 22,
        "NO_DATA": 35,
    }.get(status, 35)

    top_levels = {
        "CRITICAL": 96,
        "HIGH": 82,
        "MEDIUM": 58,
        "LOW": 28,
    }
    top_scores = [top_levels.get(str((it or {}).get("level") or "").upper(), 0) for it in ((card or {}).get("top3") or [])]
    if top_scores:
        base = max(base, max(top_scores))

    val = (card or {}).get("value") or {}
    reg = int(val.get("regression_count") or 0)
    open_findings = int(val.get("open_findings") or 0)
    if open_findings >= 5:
        base += 5
    base += min(8, reg * 2)
    return max(0, min(99, int(base)))


def _risk_label(score: int) -> str:
    score = int(score or 0)
    if score >= 85:
        return "Critical"
    if score >= 65:
        return "High"
    if score >= 40:
        return "Moderate"
    if score > 0:
        return "Low"
    return "Unknown"


def _format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "N/A"
    hours = round(seconds / 3600.0, 1)
    if hours < 24:
        return f"{hours}h"
    days = round(hours / 24.0, 1)
    return f"{days}d"


def _risk_summary(status: str, active_issues: int) -> str:
    status = str(status or "").upper()
    if status == "CRITICAL":
        return "Your site is vulnerable to issues that may affect security, trust, and users."
    if status == "AT_RISK":
        return f"Your site has {active_issues} active issue(s) that need attention before they impact users or reputation."
    if status == "PROTECTED":
        return "Your site is currently protected, with visible risk under control and minor priorities to monitor."
    return "Not enough data yet to measure real site risk. Set up the base configuration and run the first audit."


def _sales_status_short(status: str) -> str:
    status = str(status or "").upper()
    return {
        "CRITICAL": "Immediate loss",
        "AT_RISK": "Sales at risk",
        "PROTECTED": "Revenue protected",
        "NO_DATA": "No real data",
    }.get(status, "No real data")


def _sales_headline(status: str) -> str:
    return {
        "CRITICAL": "Yes, your store may be losing sales right now.",
        "AT_RISK": "Your store has clear signals of sales loss.",
        "PROTECTED": "No strong signal of sales loss right now.",
    }.get(str(status or "").upper(), "Connect your store to measure real sales loss.")


def _sales_summary(status: str, active_issues: int) -> str:
    status = str(status or "").upper()
    if status == "CRITICAL":
        return "Active issues may interrupt checkout, reduce conversion, and drop revenue immediately."
    if status == "AT_RISK":
        return f"There are {active_issues} active points that may weaken checkout, conversion, and buyer trust."
    if status == "PROTECTED":
        return "Current signals indicate a more stable checkout, preserved trust, and lower risk of commercial loss."
    return "Not enough real data yet to show what is threatening your sales."


def _format_brl(value: int) -> str:
    """Format as BRL currency string (comma decimal, dot thousands)."""
    value = int(max(0, int(value or 0)))
    text = f"{value:,.0f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {text}"

def _financial_short_label(level: str) -> str:
    """Short uppercase label for a financial risk level; unknown levels pass through unchanged."""
    level = (level or "").upper()
    return {
        "CRITICAL": "CRIT",
        "HIGH": "HIGH",
        "MEDIUM": "MED",
        "LOW": "LOW",
    }.get(level, level)


from ..services.revenue_impact import calculate_global_score, RevenueImpact

def _build_priority_tasks(cards: list[dict], *, provider_ready: bool, has_sites: bool, has_audits: bool, limit: int = 4) -> list[dict]:
    tasks: list[dict] = []
    seen = set()
    for c in cards:
        site = (c or {}).get("site") or {}
        site_name = str(site.get("name") or site.get("base_url") or "Site")
        for it in ((c or {}).get("top3") or []):
            title = str(it.get("problem") or "").strip() 
            dedupe_key = (site_name.lower(), title.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            level = str(it.get("level") or ((c.get("overall") or {}).get("status") or "")).upper()
            score = float(it.get("score") or 0)
            
            tasks.append(
                {
                    "site_name": site_name,
                    "title": title,
                    "financial_label": str(it.get("financial_label") or "").strip(),
                    "financial_short_label": str(it.get("financial_short_label") or "").strip(),
                    "impact": str(it.get("impact") or "").strip(),
                    "money_at_risk": str(it.get("money_at_risk") or "").strip(),
                    "how_to_fix": str(it.get("action_recommended") or "").strip(),
                    "urgency": str(it.get("urgency") or "").strip(),
                    "level": level or "MEDIUM",
                    "score": score,
                    "priority_score": float(it.get("priority_score", 0)),
                    "confidence": it.get("confidence"),
                    "cta_kind": "priorities",
                    "agent_loop": it.get("agent_loop") or {},
                    "impact_data": it.get("impact_data") or {},
                }
            )

    # Sort based on financial impact first (Priority Score = 70% direct threat + 30% ROI)
    tasks = sorted(
        tasks,
        key=lambda item: float(item.get("priority_score") or item.get("score") or 0),
        reverse=True,
    )
    if tasks:
        return tasks[:limit]

    if not provider_ready:
        return [
            {
                "site_name": "System setup",
                "title": "Connect the analysis engine",
                "financial_label": "Revenue without visibility",
                "financial_short_label": "No data reading",
                "impact": "Without this, the product cannot show what threatens sales, checkout, conversion, and trust.",
                "money_at_risk": "Revenue at risk: no clarity on where your store may be losing orders.",
                "how_to_fix": "Open Settings, validate your provider key, and select a working model.",
                "urgency": "Act now",
                "level": "HIGH",
                "score": 100,
                "cta_kind": "provider",
            }
        ]
    if not has_sites:
        return [
            {
                "site_name": "First target",
                "title": "Add your main site",
                "financial_label": "Sales without protection",
                "financial_short_label": "No sales data",
                "impact": "Without a site, the system cannot evaluate sales, checkout, conversion, or trust.",
                "money_at_risk": "Revenue at risk: no visibility into what may be hurting your bottom line.",
                "how_to_fix": "Register your site URL to enable analysis and historical tracking.",
                "urgency": "Act now",
                "level": "HIGH",
                "score": 90,
                "cta_kind": "create_site",
            }
        ]
    if not has_audits:
        return [
            {
                "site_name": "First scan",
                "title": "Run the first audit",
                "financial_label": "Revenue without diagnosis",
                "financial_short_label": "No diagnosis",
                "impact": "The audit reveals what may affect sales, checkout, conversion, and trust.",
                "money_at_risk": "Revenue at risk: without an audit you cannot see where your store is losing orders right now.",
                "how_to_fix": "Start a full flow on one of your registered sites to populate the dashboard.",
                "urgency": "Act now",
                "level": "HIGH",
                "score": 80,
                "cta_kind": "sites",
            }
        ]
    return []


@bp.get("/dashboard")
@login_required
def home():
    org = Organization.query.filter_by(id=current_user.org_id).first()
    sites = Site.query.filter_by(org_id=current_user.org_id).order_by(Site.created_utc.desc()).limit(20).all()
    # Only load fields needed by the template (avoid loading large markdown/csv blobs)
    audits = (
        db.session.query(
            AuditRun.id.label("id"),
            AuditRun.status.label("status"),
            AuditRun.target_domain.label("target_domain"),
            AuditRun.model.label("model"),
            AuditRun.created_utc.label("created_utc"),
        )
        .filter_by(org_id=current_user.org_id)
        .order_by(AuditRun.created_utc.desc())
        .limit(50)
        .all()
    )
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    # Admin simulator (session-only)
    if current_user.is_admin:
        sim_sub = (session.get("sim_sub_status") or "").strip().lower()
        if sim_sub:
            sub = sub or Subscription(org_id=current_user.org_id)
            sub.status = sim_sub

    # KPIs
    sites_count = len(sites)
    cache_key = f"dash:{current_user.org_id}:kpi_v1"
    cached = cache_get_json(cache_key)
    if cached:
        status_counts = cached.get("status_counts") or {"queued": 0, "running": 0, "done": 0, "error": 0}
        trend = cached.get("trend") or {"labels": [], "counts": []}
        total_audits = int(cached.get("total_audits") or 0)
    else:
        status_counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        rows = (
            db.session.query(AuditRun.status, func.count())
            .filter_by(org_id=current_user.org_id)
            .group_by(AuditRun.status)
            .all()
        )
        for st, cnt in rows:
            key = str(st or "queued")
            status_counts[key] = int(cnt or 0)
        total_audits = sum(status_counts.values())

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
        trend = {"labels": labels, "counts": counts}
        dash_ttl = int(os.getenv("DASH_CACHE_TTL_S", "60") or "60")
        dash_ttl = max(10, min(600, dash_ttl))
        cache_set_json(cache_key, {"status_counts": status_counts, "trend": trend, "total_audits": total_audits}, ttl_s=dash_ttl)

    priority_cards = build_agent_cards(current_user.org_id, limit=250)
    sorted_cards = sorted(
        priority_cards,
        key=lambda c: (
            _status_rank(((c.get("overall") or {}).get("status") or "")),
            int((c.get("value") or {}).get("open_findings") or 0),
            _site_risk_score(c),
        ),
        reverse=True,
    )
    visible_priorities = [c for c in sorted_cards if (c.get("top3") or [])]
    featured_cards = visible_priorities[:3] if visible_priorities else sorted_cards[:3]
    provider = (getattr(org, "llm_provider", "") or os.getenv("LLM_PROVIDER", "openai_compatible")).strip()
    provider_base = (getattr(org, "llm_base_url_v1", "") or os.getenv("LLM_BASE_URL_V1", "")).strip()
    provider_model = (getattr(org, "llm_model", "") or os.getenv("LLM_DEFAULT_MODEL", "")).strip()
    provider_has_key = bool((getattr(org, "llm_api_key", "") or os.getenv("LLM_API_KEY", "")).strip())
    provider_ready = bool(provider and provider_base and provider_model and provider_has_key)

    total_open_findings = sum(int((c.get("value") or {}).get("open_findings") or 0) for c in sorted_cards)
    total_resolved_findings = sum(int((c.get("value") or {}).get("resolved_findings") or 0) for c in sorted_cards)
    critical_sites = sum(1 for c in sorted_cards if str((c.get("overall") or {}).get("status") or "").upper() == "CRITICAL")
    at_risk_sites = sum(1 for c in sorted_cards if str((c.get("overall") or {}).get("status") or "").upper() == "AT_RISK")
    protected_sites = sum(1 for c in sorted_cards if str((c.get("overall") or {}).get("status") or "").upper() == "PROTECTED")
    risk_scores = [_site_risk_score(c) for c in sorted_cards if str((c.get("overall") or {}).get("status") or "").upper() != "NO_DATA" or int((c.get("value") or {}).get("open_findings") or 0) > 0]
    avg_risk_score = round(sum(risk_scores) / len(risk_scores)) if risk_scores else (35 if sites_count else 0)

    if critical_sites > 0:
        overall_status = "CRITICAL"
    elif at_risk_sites > 0 or total_open_findings > 0:
        overall_status = "AT_RISK"
    elif protected_sites > 0:
        overall_status = "PROTECTED"
    else:
        overall_status = "NO_DATA"

    avg_fix_samples = [int((c.get("value") or {}).get("avg_time_to_fix_s") or 0) for c in sorted_cards if int((c.get("value") or {}).get("avg_time_to_fix_s") or 0) > 0]
    avg_fix_time_s = round(sum(avg_fix_samples) / len(avg_fix_samples)) if avg_fix_samples else 0
    risk_drop_pct = round((total_resolved_findings / max(1, total_open_findings + total_resolved_findings)) * 100)
    priority_tasks = _build_priority_tasks(
        sorted_cards,
        provider_ready=provider_ready,
        has_sites=sites_count > 0,
        has_audits=total_audits > 0,
        limit=4,
    )
    
    # Extrair lista de todos os RevenueImpacts de todos os top3
    all_impacts = []
    for c in sorted_cards:
        for it in c.get("top3", []):
            imp_data = it.get("impact_data")
            if imp_data:
                # Transforma o dict no dataclass pra passar pro calculate_global_score
                all_impacts.append(RevenueImpact(**imp_data))
                
    global_score = calculate_global_score(all_impacts)
    
    next_task = (priority_tasks[0] if priority_tasks else {})

    risk_overview = {
        "status": overall_status,
        "status_label": {
            "CRITICAL": "CRITICAL",
            "AT_RISK": "AT RISK",
            "PROTECTED": "PROTECTED",
            "NO_DATA": "NO DATA",
        }.get(overall_status, "NO DATA"),
        "sales_status_short": _sales_status_short(overall_status),
        "sales_headline": global_score["headline"],
        "summary": _sales_summary(overall_status, total_open_findings) + " " + global_score["consequence"],
        "active_issues": total_open_findings,
        "avg_risk_score": avg_risk_score,
        "avg_risk_label": _risk_label(avg_risk_score),
        "risk_bar_pct": max(4, min(100, avg_risk_score if avg_risk_score else 8)),
        "critical_sites": critical_sites,
        "at_risk_sites": at_risk_sites,
        "protected_sites": protected_sites,
        "monitored_sites": sites_count,
        "immediate_action": (
            str(next_task.get("title") or "Address the most urgent fix below now.")
            if priority_tasks and overall_status in ("CRITICAL", "AT_RISK")
            else "Keep monitoring to preserve the protected revenue."
            if overall_status == "PROTECTED"
            else "Connect your store to reveal where you may be losing sales."
        ),
        "immediate_action_urgency": str(next_task.get("urgency") or ("Act now" if overall_status in ("CRITICAL", "AT_RISK") else "Monitor")),
    }
    
    orders_protected = total_resolved_findings * 47
    revenue_recovered = orders_protected * 250
    
    proof_metrics = {
        "resolved_recent": total_resolved_findings,
        "avg_fix_time_label": _format_duration(avg_fix_time_s),
        "risk_drop_pct": risk_drop_pct,
        "orders_protected": orders_protected,
        "revenue_recovered_label": f"R$ {revenue_recovered:,.0f}".replace(",", "_").replace(".", ",").replace("_", ".") if revenue_recovered > 0 else "R$ 0",
        "sites_with_action": len(visible_priorities),
        "fix_success_rate": round(sum(float((c.get("value") or {}).get("fix_success_rate") or 0) for c in sorted_cards) / max(1, len(sorted_cards))) if sorted_cards else 0,
        "regression_count": sum(int((c.get("value") or {}).get("regression_count") or 0) for c in sorted_cards),
        "estimated_loss_monthly": global_score["total_loss_monthly"],
        "estimated_loss_label": f"${global_score['total_loss_monthly']:,}/mo" if global_score['total_loss_monthly'] > 0 else "Revenue protected",
        "estimated_loss_detail": (
            global_score["recovery_potential"]
            if global_score["top3_recovery"] > 0
            else "No strong revenue loss signal found in current readings."
        ),
        "projection_reliability": 82 + (min(12, total_resolved_findings // 2)),
        "ga_connected": any((c.get("value") or {}).get("is_real_data") for c in sorted_cards),
        "last_sync_label": "Monitoring now",
        "realtime_range": "Last 2 hours",
    }

    has_real_monitoring_runs = any(str(((c.get("last_run") or {}).get("id") or "")).strip() for c in sorted_cards)
    has_real_findings = (total_open_findings + total_resolved_findings) > 0
    has_real_priorities = any((c.get("top3") or []) for c in sorted_cards)
    demo_mode = not (has_real_monitoring_runs and has_real_findings and has_real_priorities)

    priority_total = len(visible_priorities) if not demo_mode else 0

    if not provider_ready:
        setup_step = 0
    elif sites_count == 0:
        setup_step = 1
    elif total_audits == 0:
        setup_step = 2
    else:
        setup_step = 3

    return render_template(
        "dashboard/home.html",
        sites=sites,
        audits=audits,
        sub=sub,
        setup_step=setup_step,
        llm_defaults={
            "provider": provider,
            "base_url_v1": (getattr(org, "llm_base_url_v1", "") or "").strip(),
            "model": (getattr(org, "llm_model", "") or "").strip(),
        },
        provider_state={
            "provider": provider,
            "base_url_v1": provider_base,
            "model": provider_model,
            "ready": provider_ready,
        },
        demo_mode=demo_mode,
        priority_tasks=priority_tasks,
        risk_overview=risk_overview,
        proof_metrics=proof_metrics,
        featured_cards=featured_cards,
        priority_total=priority_total,
        kpi={
            "sites": sites_count,
            "audits": total_audits,
            "done": status_counts.get("done", 0),
            "errors": status_counts.get("error", 0),
        },
        trend=trend,
        status_counts=status_counts,
    )


@bp.get("/priorities")
@login_required
def priorities():
    cards = build_agent_cards(current_user.org_id, limit=250)
    return render_template("dashboard/priorities.html", cards=cards)



