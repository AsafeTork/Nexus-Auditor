from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import text

from .. import db
from ..models import Site


def get_site_agent_state(org_id: str, site_id: str) -> Dict[str, Any]:
    """
    Minimal Agent Control Plane (read-only).
    Returns the latest decision_json for a given (org_id, site_id).

    Implementation is SQL-text based to avoid hard-depending on optional monitoring models.
    """
    if not org_id or not site_id:
        return {}

    try:
        rr = db.session.execute(
            text(
                """
                SELECT decision_json
                FROM monitoring_runs
                WHERE org_id = :org_id AND site_id = :site_id
                ORDER BY created_utc DESC
                LIMIT 1
                """
            ),
            {"org_id": org_id, "site_id": site_id},
        ).mappings().first()
    except Exception:
        return {}

    raw = str((rr or {}).get("decision_json") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_agent_cards(org_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Read-only aggregation for /admin/agent.
    Data sources (read-only):
      - sites
      - monitoring_runs (decision_json, verification_json)
      - monitoring_findings (open/resolved/avg_time/regressions)
      - site_contexts (coverage/strictness/instability)
    """
    org_id = (org_id or "").strip()
    if not org_id:
        return []

    limit = max(1, min(500, int(limit or 200)))
    sites = Site.query.filter_by(org_id=org_id).order_by(Site.created_utc.desc()).limit(limit).all()

    # Aggregates from monitoring_findings (best-effort; table may not exist).
    agg_map: Dict[str, Dict[str, Any]] = {}
    try:
        rows = db.session.execute(
            text(
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
            ),
            {"org_id": org_id},
        ).mappings().all()
        for r in rows:
            sid = str(r.get("site_id") or "")
            if not sid:
                continue
            agg_map[sid] = {
                "open_findings": int(r.get("open_count") or 0),
                "resolved_findings": int(r.get("resolved_count") or 0),
                "regression_count": int(r.get("regression_count") or 0),
                "avg_time_to_fix_s": int(r.get("avg_time_to_fix_s") or 0),
            }
    except Exception:
        agg_map = {}

    # Context map (best-effort; table may not exist).
    ctx_map: Dict[str, Dict[str, Any]] = {}
    try:
        ctx_rows = db.session.execute(
            text(
                """
                SELECT site_id, coverage_quality, instability_score, complexity
                FROM site_contexts
                WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
        for r in ctx_rows:
            sid = str(r.get("site_id") or "")
            if not sid:
                continue
            # strictness may not exist in older schemas; compute conservative strictness from signals.
            cov = str(r.get("coverage_quality") or "N/A").upper()
            inst = int(r.get("instability_score") or 0)
            comp = str(r.get("complexity") or "MEDIUM").upper()
            strict = 0
            if comp == "HIGH":
                strict += 35
            elif comp == "MEDIUM":
                strict += 15
            if cov == "LOW":
                strict += 45
            elif cov == "MEDIUM":
                strict += 20
            strict += int(inst * 0.4)
            strict = max(0, min(100, strict))
            ctx_map[sid] = {
                "coverage_quality": cov,
                "instability_score": inst,
                "strictness": strict,
            }
    except Exception:
        ctx_map = {}

    cards: List[Dict[str, Any]] = []
    for s in sites:
        sid = s.id
        agg = agg_map.get(sid, {"open_findings": 0, "resolved_findings": 0, "regression_count": 0, "avg_time_to_fix_s": 0})
        ctx = ctx_map.get(sid, {"coverage_quality": "N/A", "strictness": 0, "instability_score": 0})

        last_run = None
        try:
            last_run = db.session.execute(
                text(
                    """
                    SELECT id, created_utc, status, decision_json, verification_json
                    FROM monitoring_runs
                    WHERE org_id = :org_id AND site_id = :site_id
                    ORDER BY created_utc DESC
                    LIMIT 1
                    """
                ),
                {"org_id": org_id, "site_id": sid},
            ).mappings().first()
        except Exception:
            last_run = None

        has_decision = bool(str((last_run or {}).get("decision_json") or "").strip())
        has_verification = bool(str((last_run or {}).get("verification_json") or "").strip())

        decision = {}
        if has_decision:
            try:
                decision = json.loads(str(last_run.get("decision_json") or "")) or {}
            except Exception:
                decision = {}

        top = decision.get("top") or []
        if not isinstance(top, list):
            top = []
        top3 = top[:3]

        # Overall confidence: avg of top item confidences.
        conf_vals = []
        for it in top3:
            try:
                conf_vals.append(float(it.get("confidence")))
            except Exception:
                pass
        confidence = round(sum(conf_vals) / len(conf_vals), 3) if conf_vals else None

        # Status classification (conservative, deterministic).
        if not last_run and (agg.get("open_findings") == 0) and (ctx.get("coverage_quality") == "N/A"):
            overall_status = "NO_DATA"
        else:
            max_level = ""
            for it in top3:
                lvl = str(it.get("level") or "").upper()
                if lvl in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                    if lvl == "CRITICAL":
                        max_level = "CRITICAL"
                        break
                    if lvl == "HIGH" and max_level not in ("CRITICAL",):
                        max_level = "HIGH"
                    if lvl == "MEDIUM" and max_level not in ("CRITICAL", "HIGH"):
                        max_level = "MEDIUM"
                    if lvl == "LOW" and not max_level:
                        max_level = "LOW"

            open_n = int(agg.get("open_findings") or 0)
            reg_n = int(agg.get("regression_count") or 0)
            strict = int(ctx.get("strictness") or 0)
            inst = int(ctx.get("instability_score") or 0)
            cov = str(ctx.get("coverage_quality") or "N/A").upper()

            if open_n > 0 and (max_level == "CRITICAL" or reg_n >= 3 or inst >= 70):
                overall_status = "CRITICAL"
            elif open_n > 0 or reg_n > 0 or strict >= 70 or cov == "LOW":
                overall_status = "AT_RISK"
            else:
                overall_status = "PROTECTED"

        denom = max(1, int(agg.get("open_findings") or 0) + int(agg.get("resolved_findings") or 0))
        fix_success_rate = round((int(agg.get("resolved_findings") or 0) / denom) * 100.0, 2)

        def _summ_action_line(it: Dict[str, Any]) -> str:
            ab = it.get("action") or {}
            title = str(ab.get("title") or "").strip()
            if title:
                return title
            rec = str(it.get("recommendation") or "").strip()
            return rec[:140]

        compact_top = []
        for it in top3:
            sg = it.get("safety_gate") or {}
            rs = sg.get("reasons") or []
            if not isinstance(rs, list):
                rs = []
            compact_top.append(
                {
                    "finding_key": it.get("key") or "",
                    "score": it.get("score"),
                    "level": it.get("level"),
                    "confidence": it.get("confidence"),
                    "safety_gate": {
                        "status": (sg.get("status") or "REQUIRES_CONFIRMATION"),
                        "reasons": [str(x) for x in rs[:2]],
                    },
                    "action_line": _summ_action_line(it),
                }
            )

        cards.append(
            {
                "site": {"id": sid, "name": s.name, "base_url": s.base_url},
                "last_run": {
                    "id": str((last_run or {}).get("id") or ""),
                    "created_utc": str((last_run or {}).get("created_utc") or ""),
                    "status": str((last_run or {}).get("status") or ""),
                },
                "overall": {
                    "status": overall_status,
                    "confidence": confidence,
                    "coverage_quality": ctx.get("coverage_quality"),
                    "strictness": ctx.get("strictness"),
                    "instability_score": ctx.get("instability_score"),
                },
                "top3": compact_top,
                "value": {
                    "open_findings": int(agg.get("open_findings") or 0),
                    "resolved_findings": int(agg.get("resolved_findings") or 0),
                    "fix_success_rate": fix_success_rate,
                    "avg_time_to_fix_s": int(agg.get("avg_time_to_fix_s") or 0),
                    "regression_count": int(agg.get("regression_count") or 0),
                },
                "flags": {"has_decision_json": has_decision, "has_verification_json": has_verification},
                "trust_warning": bool(str(ctx.get("coverage_quality") or "").upper() == "LOW"),
            }
        )

    return cards
