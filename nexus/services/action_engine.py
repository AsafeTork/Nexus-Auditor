from __future__ import annotations

import re
from typing import Dict

from .finding_types import Finding


def _classify_kind(f: Finding) -> str:
    """
    Deterministic action classification by finding content.
    Must be stable, explainable, and not rely on external APIs.
    """
    text = " ".join([f.category or "", f.failure or "", f.solution or "", f.explanation or ""])
    t = text.lower()

    if "strict-transport-security" in t or "hsts" in t:
        return "headers_hsts"
    if "content-security-policy" in t or "csp" in t:
        return "headers_csp"
    if "cors" in t:
        return "cors_policy"
    if re.search(r"\bsqli\b|sql injection", t):
        return "sqli"
    if re.search(r"\bxss\b|cross[- ]site", t):
        return "xss"
    if "csrf" in t:
        return "csrf"
    if "rate limit" in t or "throttle" in t:
        return "rate_limit"
    if "cookie" in t and ("secure" in t or "httponly" in t or "samesite" in t):
        return "cookie_hardening"

    cat = (f.category or "").lower()
    if "headers" in cat or "infra" in cat or "ssl" in cat or "tls" in cat:
        return "infra_generic"
    if "segurança" in cat or "security" in cat or "vulnerab" in cat:
        return "security_generic"
    return "generic"


def generate_action_block(f: Finding) -> Dict:
    """
    Returns a safe, reversible, scoped "action block" for a finding.
    Constraints:
      - MUST NOT execute anything
      - deterministic mapping
      - include rollback guidance where possible
    """
    kind = _classify_kind(f)

    # Defaults (manual, safe)
    action = {
        "kind": kind,
        "classification": "MANUAL_REQUIRED",
        "title": "Manual remediation required",
        "scope": "Application / config change (review required)",
        "steps": [
            "Apply the recommended fix in a staging environment first.",
            "Add a regression test that reproduces the issue and confirms the fix.",
            "Re-run monitoring to verify the outcome (FIX VERIFIED).",
        ],
        "snippet_language": "text",
        "snippet": (f.solution or "").strip() or "No deterministic snippet available for this finding type.",
        "rollback": "Revert the change via version control and redeploy.",
        "safety_notes": [
            "Never apply changes directly in production without a rollback plan.",
            "Prefer small, scoped diffs and verify with monitoring.",
        ],
    }

    if kind == "headers_hsts":
        action.update(
            {
                "classification": "SAFE_AUTOMATIC",
                "title": "Add HSTS header at the edge (recommended)",
                "scope": "CDN / reverse proxy (site-wide)",
                "snippet_language": "nginx",
                "snippet": (
                    "## Nginx (recommended)\n"
                    "add_header Strict-Transport-Security \"max-age=31536000; includeSubDomains; preload\" always;\n\n"
                    "## Verification\n"
                    "curl -I https://YOUR_DOMAIN | grep -i strict-transport-security\n"
                ),
                "rollback": "Remove the Strict-Transport-Security header line and redeploy/reload Nginx.",
                "safety_notes": [
                    "Enable only after HTTPS is stable across all subdomains.",
                    "Misconfig can lock clients to HTTPS; test staging first.",
                ],
            }
        )

    elif kind == "headers_csp":
        action.update(
            {
                "classification": "MANUAL_REQUIRED",
                "title": "Introduce CSP in Report-Only, then enforce",
                "scope": "HTTP header (site-wide, can break scripts)",
                "snippet_language": "nginx",
                "snippet": (
                    "## Phase 1: Report-Only (safe rollout)\n"
                    "add_header Content-Security-Policy-Report-Only \"default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'\" always;\n\n"
                    "## Phase 2: Enforce (after observing reports)\n"
                    "# add_header Content-Security-Policy \"default-src 'self'; ...\" always;\n"
                ),
                "rollback": "Remove the CSP header and redeploy/reload; use Report-Only first to minimize risk.",
                "safety_notes": [
                    "CSP can break third-party scripts; start with Report-Only.",
                    "Collect CSP violation reports before enforcing.",
                ],
            }
        )

    elif kind == "cors_policy":
        action.update(
            {
                "classification": "MANUAL_REQUIRED",
                "title": "Restrict CORS origins to explicit allowlist",
                "scope": "API responses (per route / per service)",
                "snippet_language": "python",
                "snippet": (
                    "# Flask example (flask-cors)\n"
                    "# from flask_cors import CORS\n"
                    "# CORS(app, resources={r\"/api/*\": {\"origins\": [\"https://app.yourdomain.com\"]}})\n"
                ),
                "rollback": "Restore previous CORS configuration and redeploy.",
                "safety_notes": [
                    "Avoid wildcard origins for authenticated endpoints.",
                    "Validate preflight behavior for required clients.",
                ],
            }
        )

    elif kind == "rate_limit":
        action.update(
            {
                "classification": "SAFE_AUTOMATIC",
                "title": "Add rate limiting to reduce abuse surface",
                "scope": "Reverse proxy / gateway (recommended) or app middleware",
                "snippet_language": "nginx",
                "snippet": (
                    "## Nginx example (per-IP limit)\n"
                    "limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;\n"
                    "server {\n"
                    "  location /api/ {\n"
                    "    limit_req zone=api_limit burst=20 nodelay;\n"
                    "  }\n"
                    "}\n"
                ),
                "rollback": "Remove limit_req_* config and reload Nginx.",
                "safety_notes": [
                    "Ensure legitimate traffic patterns are not blocked; tune rate/burst.",
                    "Prefer per-endpoint limits for sensitive routes.",
                ],
            }
        )

    elif kind == "cookie_hardening":
        action.update(
            {
                "classification": "SAFE_AUTOMATIC",
                "title": "Harden session cookies (Secure/HttpOnly/SameSite)",
                "scope": "Application cookie settings",
                "snippet_language": "python",
                "snippet": (
                    "# Flask config example\n"
                    "SESSION_COOKIE_SECURE = True\n"
                    "SESSION_COOKIE_HTTPONLY = True\n"
                    "SESSION_COOKIE_SAMESITE = \"Lax\"  # or \"Strict\" if compatible\n"
                ),
                "rollback": "Revert cookie flags and redeploy.",
                "safety_notes": [
                    "SameSite=Strict can break OAuth flows; validate login flows.",
                ],
            }
        )

    elif kind == "sqli":
        action.update(
            {
                "classification": "MANUAL_REQUIRED",
                "title": "Fix SQL injection risk (parameterize queries)",
                "scope": "Application code (specific query)",
                "snippet_language": "python",
                "snippet": (
                    "# SQLAlchemy example\n"
                    "# BAD: session.execute(f\"SELECT * FROM users WHERE id={user_id}\")\n"
                    "# GOOD:\n"
                    "from sqlalchemy import text\n"
                    "row = db.session.execute(text(\"SELECT * FROM users WHERE id=:id\"), {\"id\": user_id}).fetchone()\n"
                ),
                "rollback": "Revert the code change and redeploy.",
                "safety_notes": [
                    "Add regression tests covering malicious payloads.",
                    "Validate authorization (IDOR) separately from injection.",
                ],
            }
        )

    elif kind == "xss":
        action.update(
            {
                "classification": "MANUAL_REQUIRED",
                "title": "Fix XSS risk (encode output + validate input)",
                "scope": "Template rendering / frontend output",
                "snippet_language": "text",
                "snippet": (
                    "- Ensure templates escape output by default.\n"
                    "- Never mark user-controlled HTML as safe.\n"
                    "- Add an allowlist sanitizer only if HTML input is required.\n"
                ),
                "rollback": "Revert the rendering change and redeploy.",
                "safety_notes": [
                    "Avoid DIY sanitizers; use allowlists and test payloads.",
                ],
            }
        )

    elif kind == "csrf":
        action.update(
            {
                "classification": "MANUAL_REQUIRED",
                "title": "Add CSRF protection to state-changing endpoints",
                "scope": "Forms / authenticated POST endpoints",
                "snippet_language": "text",
                "snippet": (
                    "- Ensure CSRF tokens are required on all authenticated POST/PUT/DELETE.\n"
                    "- Validate SameSite cookies and Origin/Referer for sensitive actions.\n"
                ),
                "rollback": "Revert CSRF enforcement only if it blocks legitimate flows; fix clients/forms properly.",
                "safety_notes": [
                    "Do not disable CSRF globally; scope exceptions explicitly.",
                ],
            }
        )

    return action
