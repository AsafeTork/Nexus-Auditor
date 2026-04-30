from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .. import db
from ..models import SitePolicy


def _risk_rank(level: str) -> int:
    m = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    return m.get((level or "").strip().upper(), 2)


@dataclass
class GateResult:
    status: str  # ALLOWED|BLOCKED|REQUIRES_CONFIRMATION
    reasons: List[str]
    action: dict


def load_site_policy(org_id: str, site_id: str) -> SitePolicy:
    """
    Get-or-create site policy with strict, safe defaults.
    """
    pol = SitePolicy.query.filter_by(org_id=org_id, site_id=site_id).first()
    if pol:
        return pol
    pol = SitePolicy(
        org_id=org_id,
        site_id=site_id,
        # Safe defaults:
        allow_auto_apply=False,
        max_risk_level=os.getenv("POLICY_DEFAULT_MAX_RISK_LEVEL", "HIGH").strip().upper() or "HIGH",
        enforce_csp_report_only=True,
        max_rate_limit_rps=int(os.getenv("POLICY_DEFAULT_MAX_RATE_RPS", "20") or "20"),
    )
    db.session.add(pol)
    db.session.commit()
    return pol


def _parse_json_list(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        pass
    # fallback: CSV
    return [p.strip() for p in raw.split(",") if p.strip()]


def safety_gate(
    *,
    action_block: dict,
    finding_level: str,
    policy: SitePolicy,
    context: dict | None = None,
) -> GateResult:
    """
    Deterministic safety gating.
    - No action can bypass this check.
    - May sanitize/downgrade action.
    """
    reasons: List[str] = []
    action = dict(action_block or {})
    kind = str(action.get("kind") or "unknown")

    allowed = _parse_json_list(policy.allowed_action_kinds_json)
    forbidden = _parse_json_list(policy.forbidden_action_kinds_json)

    # Hard block forbidden
    if kind in forbidden:
        return GateResult(status="BLOCKED", reasons=[f"Action kind '{kind}' is forbidden by policy."], action=action)

    # Allow list (if configured)
    if allowed and kind not in allowed:
        return GateResult(status="BLOCKED", reasons=[f"Action kind '{kind}' is not in allowed list."], action=action)

    # Must have rollback plan
    if not str(action.get("rollback") or "").strip():
        return GateResult(status="BLOCKED", reasons=["Missing rollback plan."], action=action)

    status = "ALLOWED"
    context = context or {}
    strictness = int(context.get("strictness") or 0)
    complexity = str(context.get("complexity") or "").upper()
    coverage_quality = str(context.get("coverage_quality") or "").upper()
    instability = int(context.get("instability_score") or 0)

    # Risk-level based confirmation requirement
    if _risk_rank(finding_level) > _risk_rank(policy.max_risk_level):
        status = "REQUIRES_CONFIRMATION"
        reasons.append(f"Finding risk level {finding_level} exceeds policy max {policy.max_risk_level}.")

    # Contextual strictness overrides (deterministic)
    if strictness >= 70:
        status = "REQUIRES_CONFIRMATION"
        reasons.append(f"Context strictness={strictness} requires confirmation (complexity/coverage/instability).")
    if strictness >= 90:
        # Never block by default, but downgrade to manual and force confirmation
        action["classification"] = "MANUAL_REQUIRED"
        status = "REQUIRES_CONFIRMATION"
        reasons.append("Very high strictness: downgraded to MANUAL_REQUIRED.")

    # Auto-apply protection: never allow SAFE_AUTOMATIC without explicit policy
    if str(action.get("classification") or "") == "SAFE_AUTOMATIC" and not bool(policy.allow_auto_apply):
        # Not blocked, but ensure it cannot be treated as auto-apply in future.
        action["classification"] = "MANUAL_REQUIRED"
        status = "REQUIRES_CONFIRMATION"
        reasons.append("Policy disables auto-apply; action downgraded to MANUAL_REQUIRED.")

    # Enforcement rule: CSP must start report-only
    if bool(policy.enforce_csp_report_only) and kind == "headers_csp":
        snip = str(action.get("snippet") or "")
        # If enforce header exists, remove it (keep report-only).
        # This avoids accidental prod breakage.
        if "Content-Security-Policy \"" in snip and "Report-Only" not in snip:
            snip = re.sub(r"add_header\s+Content-Security-Policy\s+\".*?\".*?\n", "", snip, flags=re.I)
            action["snippet"] = snip.strip() + "\n"
            status = "REQUIRES_CONFIRMATION"
            reasons.append("CSP enforcement removed; policy requires Report-Only first.")
        # If snippet includes both phases, still enforce staged rollout:
        if "Report-Only" not in snip:
            status = "REQUIRES_CONFIRMATION"
            reasons.append("CSP action must be staged (Report-Only).")

        # Extra contextual rule: high complexity => always restrict CSP (no enforcement hints)
        if complexity == "HIGH":
            status = "REQUIRES_CONFIRMATION"
            reasons.append("High site complexity: CSP changes require staged rollout + manual review.")

    # Enforcement rule: cap rate limit
    if kind == "rate_limit":
        max_rps = int(policy.max_rate_limit_rps or 20)
        max_rps = max(1, min(1000, max_rps))
        snip = str(action.get("snippet") or "")
        # Replace rate=Nr/s with capped rate
        def repl(m):
            n = int(m.group(1))
            if n > max_rps:
                reasons.append(f"Rate limit capped from {n}r/s to {max_rps}r/s by policy.")
                return f"rate={max_rps}r/s"
            return m.group(0)

        snip2 = re.sub(r"rate=(\d+)r/s", repl, snip)
        if snip2 != snip:
            action["snippet"] = snip2
            status = "REQUIRES_CONFIRMATION" if status != "BLOCKED" else status

    # Enforcement rule: headers must include validation steps
    if kind.startswith("headers_") or kind in ("headers_hsts", "infra_generic"):
        snip = str(action.get("snippet") or "")
        if "curl -I" not in snip:
            action["snippet"] = (snip.strip() + "\n\n# Verification\ncurl -I https://YOUR_DOMAIN\n").strip() + "\n"
            status = "REQUIRES_CONFIRMATION"
            reasons.append("Added verification command (curl -I) for header validation.")

    # Contextual rule: low coverage => never allow "safe" classification (avoid false safety)
    if coverage_quality == "LOW":
        status = "REQUIRES_CONFIRMATION"
        reasons.append("Low scan coverage: treat action as higher risk until coverage improves.")

    return GateResult(status=status, reasons=reasons, action=action)
