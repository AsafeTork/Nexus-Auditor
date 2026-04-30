from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

from .action_engine import generate_action_block
from .finding_types import Finding


def parse_csv_findings(csv_text: str) -> List[Finding]:
    findings: List[Finding] = []
    if not csv_text:
        return findings
    for ln in csv_text.splitlines():
        row = (ln or "").strip()
        if not row:
            continue
        if row.lower().startswith("categoria;"):
            continue
        parts = [p.strip() for p in row.split(";")]
        # Expected: 8 columns, but tolerate extra semicolons.
        if len(parts) < 2:
            continue
        while len(parts) < 8:
            parts.append("")
        category, failure, proof, explanation, loss, solution, priority, complexity = parts[:8]
        key = (category.lower() + "|" + failure.lower()).strip()
        findings.append(
            Finding(
                key=key,
                category=category,
                failure=failure,
                proof=proof,
                explanation=explanation,
                loss=loss,
                solution=solution,
                priority=priority,
                complexity=complexity,
            )
        )
    return findings


def _priority_severity(priority: str) -> int:
    p = (priority or "").strip().lower()
    if p in ("crítica", "critica", "critical"):
        return 95
    if p in ("alta", "high"):
        return 80
    if p in ("média", "media", "medium"):
        return 55
    if p in ("baixa", "low"):
        return 30
    return 45


_KW_EXPL = [
    (re.compile(r"\brce\b|remote code", re.I), 1.0),
    (re.compile(r"\bsqli\b|sql injection", re.I), 0.95),
    (re.compile(r"\bxss\b|cross[- ]site", re.I), 0.8),
    (re.compile(r"\bssrf\b", re.I), 0.85),
    (re.compile(r"\bcsrf\b", re.I), 0.65),
    (re.compile(r"\bcors\b", re.I), 0.55),
    (re.compile(r"\bcsp\b|content-security-policy", re.I), 0.45),
    (re.compile(r"\bhsts\b|strict-transport-security", re.I), 0.4),
    (re.compile(r"\btls\b|ssl\b", re.I), 0.5),
    (re.compile(r"\bauth\b|login|session|cookie|jwt", re.I), 0.65),
    (re.compile(r"\bopen redirect\b", re.I), 0.55),
]


def _exploitability(f: Finding) -> float:
    text = " ".join([f.category, f.failure, f.proof, f.explanation])
    best = 0.25
    for rx, val in _KW_EXPL:
        if rx.search(text):
            best = max(best, float(val))
    # Category bias (security layers usually more exploitable)
    cat = (f.category or "").lower()
    if "segurança" in cat or "security" in cat or "vulnerab" in cat:
        best = max(best, 0.55)
    if "infra" in cat or "headers" in cat or "ssl" in cat:
        best = max(best, 0.4)
    return min(1.0, max(0.0, best))


def _exposure_surface(f: Finding) -> float:
    """
    Explainable proxy for exposure:
    - global headers/ssl issues often affect all requests
    - mentions of "all pages", "sitewide", "global", "any origin" increase exposure
    """
    text = " ".join([f.category, f.failure, f.proof, f.explanation]).lower()
    score = 0.25
    if any(k in text for k in ("all pages", "sitewide", "global", "todas as páginas", "todas as paginas")):
        score = max(score, 0.75)
    if any(k in text for k in ("any origin", "qualquer origem", "wildcard", "*")):
        score = max(score, 0.7)
    cat = (f.category or "").lower()
    if "headers" in cat or "infra" in cat or "ssl" in cat or "tls" in cat:
        score = max(score, 0.6)
    return min(1.0, max(0.0, score))


def _recurrence_bonus(recurrence_count: int) -> float:
    """
    0..1 bonus: persists across multiple monitoring runs.
    """
    if recurrence_count <= 1:
        return 0.0
    if recurrence_count == 2:
        return 0.35
    if recurrence_count == 3:
        return 0.6
    return 0.85


def score_finding(f: Finding, *, recurrence_count: int = 1) -> Dict:
    severity = _priority_severity(f.priority)
    exploit = _exploitability(f)
    exposure = _exposure_surface(f)
    recur = _recurrence_bonus(int(recurrence_count or 1))

    # Explainable weighted score (0..100)
    base_score = (
        0.45 * severity
        + 0.25 * (exploit * 100.0)
        + 0.15 * (exposure * 100.0)
        + 0.15 * (recur * 100.0)
    )
    score = int(max(0, min(100, round(base_score))))

    if score >= 85:
        level = "CRITICAL"
    elif score >= 70:
        level = "HIGH"
    elif score >= 45:
        level = "MEDIUM"
    else:
        level = "LOW"

    # Action recommendation (developer-friendly, at least one for CRITICAL)
    rec = (f.solution or "").strip()
    if not rec:
        # Minimal templates by category
        cat = (f.category or "").lower()
        if "headers" in cat or "infra" in cat:
            rec = "Configure security headers at the edge (CDN/reverse proxy) and verify via curl + browser devtools."
        elif "vulnerab" in cat or "segurança" in cat or "security" in cat:
            rec = "Add input validation + output encoding, and write a regression test that reproduces the issue."
        else:
            rec = "Implement the recommended fix and add a regression check in CI to prevent recurrence."

    # Attach evidence pointer
    evidence = (f.proof or "").strip()
    if evidence:
        rec = rec + " Evidence: " + evidence[:220]

    return {
        "key": f.key,
        "category": f.category,
        "failure": f.failure,
        "priority_raw": f.priority,
        "complexity": f.complexity,
        "severity_base": severity,
        "exploitability": round(exploit, 2),
        "exposure": round(exposure, 2),
        "recurrence_count": int(recurrence_count or 1),
        "score": score,
        "base_score": int(max(0, min(100, round(base_score)))),
        "level": level,
        "recommendation": rec,
    }


def build_decision_report(
    findings: List[Finding],
    *,
    recurrence_map: Dict[str, int],
    learning_map: Dict[str, dict] | None = None,
    policy=None,
    safety_gate_fn=None,
    context: dict | None = None,
    top_n: int = 3,
) -> Dict:
    """
    Returns a structured decision layer:
      - per-item scores + recommendations
      - top priorities list
      - scoring rubric (explainability)
    """
    top_n = max(1, min(10, int(top_n or 3)))
    learning_map = learning_map or {}
    items = []
    for f in findings:
        it = score_finding(f, recurrence_count=int(recurrence_map.get(f.key, 1)))
        hist = learning_map.get(f.key) or {}

        # Confidence (0..1) computed from historical effectiveness (explainable).
        # - higher success_rate => higher confidence
        # - higher avg_resolution_s => lower confidence
        # - higher regression_rate => lower confidence
        sr = float(hist.get("success_rate") or 0.0)
        avg_s = int(hist.get("avg_resolution_s") or 0)
        rr = float(hist.get("regression_rate") or 0.0)
        sample = int(hist.get("sample_size") or 0)
        # Normalize avg time against 7 days
        avg_norm = min(1.0, max(0.0, float(avg_s) / float(7 * 24 * 3600))) if avg_s > 0 else 0.0
        confidence = 0.55 * sr + 0.25 * (1.0 - avg_norm) + 0.20 * (1.0 - min(1.0, rr))
        # Guard against tiny data: shrink toward neutral (0.5)
        shrink = min(1.0, sample / 10.0)  # 10 samples -> full trust
        confidence = (shrink * confidence) + ((1.0 - shrink) * 0.5)
        confidence = max(0.0, min(1.0, confidence))

        # Adaptive score adjustment (still explainable):
        # Low confidence => increase score (more urgency / harder to fix)
        # High confidence => slight decrease (easy wins not always top priority)
        adj = int(round((0.5 - confidence) * 14))  # ~[-7, +7]
        it["confidence"] = round(confidence, 3)
        it["historical_effectiveness"] = {
            "success_rate": round(sr, 4),
            "avg_resolution_s": avg_s,
            "regression_rate": round(rr, 4),
            "sample_size": sample,
            "rec_kind": hist.get("rec_kind") or "unknown",
        }
        it["score_adjustment"] = adj
        it["score"] = int(max(0, min(100, int(it["score"]) + adj)))

        # Recompute level after adjustment
        s2 = int(it["score"])
        if s2 >= 85:
            it["level"] = "CRITICAL"
        elif s2 >= 70:
            it["level"] = "HIGH"
        elif s2 >= 45:
            it["level"] = "MEDIUM"
        else:
            it["level"] = "LOW"

        # Action block (safe, deterministic). Ensure CRITICAL gets a ready-to-use action.
        try:
            it["action"] = generate_action_block(f)
        except Exception:
            it["action"] = {
                "classification": "MANUAL_REQUIRED",
                "title": "Manual remediation required",
                "steps": ["Apply the fix in staging, then re-run monitoring to verify."],
            }

        # Safety & policy gate (mandatory before any future execution).
        # We keep decision_engine generic: caller supplies safety_gate_fn and policy.
        try:
            if safety_gate_fn and policy:
                gr = safety_gate_fn(
                    action_block=it.get("action") or {},
                    finding_level=str(it.get("level") or ""),
                    policy=policy,
                    context=context or {},
                )
                it["safety_gate"] = {"status": gr.status, "reasons": gr.reasons}
                it["action"] = gr.action
            else:
                it["safety_gate"] = {"status": "REQUIRES_CONFIRMATION", "reasons": ["No policy attached; default deny auto-apply."]}
                # do not alter action
        except Exception:
            it["safety_gate"] = {"status": "REQUIRES_CONFIRMATION", "reasons": ["Safety gate error; treat as confirmation required."]}

        items.append(it)
    items.sort(key=lambda x: (x.get("score", 0), x.get("level", "")), reverse=True)
    top = items[:top_n]

    rubric = {
        "score_range": "0-100",
        "weights": {"severity": 0.45, "exploitability": 0.25, "exposure": 0.15, "recurrence": 0.15},
        "adaptive_adjustment": {
            "enabled": True,
            "based_on": ["success_rate", "avg_resolution_s", "regression_rate", "sample_size"],
            "range": "approximately -7..+7 points",
            "note": "Low confidence increases urgency; high confidence slightly reduces urgency.",
        },
        "levels": {"CRITICAL": ">=85", "HIGH": "70-84", "MEDIUM": "45-69", "LOW": "<45"},
        "notes": [
            "Severity comes from the CSV Priority column (Critical/High/Medium/Low).",
            "Exploitability and exposure are keyword/category heuristics (explainable).",
            "Recurrence boosts findings that persist across monitoring runs.",
            "Confidence is learned from verified outcomes and adjusts the final score slightly (explainable).",
        ],
    }

    return {"top": top, "items": items, "rubric": rubric}


def decision_markdown(decision: Dict) -> str:
    top = decision.get("top") or []
    if not top:
        return "\n\n## Decision engine\n- No findings to prioritize.\n"

    lines: List[str] = []
    lines.append("\n\n## Decision engine (priorities)\n")
    lines.append("Top priorities (what to fix first):\n")
    for i, t in enumerate(top, start=1):
        lines.append(
            f"{i}. [{t.get('level')}] score={t.get('score')} — {t.get('category')}: {t.get('failure')}"
        )
        conf = t.get("confidence", None)
        if conf is not None:
            lines.append(f"   - Confidence: {conf} (learned from past outcomes)")
        rec = (t.get("recommendation") or "").strip()
        if rec:
            lines.append(f"   - Action: {rec}")
        ab = t.get("action") or {}
        if ab:
            lines.append(f"   - Fix block: {ab.get('classification','MANUAL_REQUIRED')} — {ab.get('title','')}".rstrip())
        sg = t.get("safety_gate") or {}
        if sg:
            lines.append(f"   - Safety gate: {sg.get('status','REQUIRES_CONFIRMATION')}")
            rs = sg.get("reasons") or []
            for r in rs[:3]:
                lines.append(f"     - {r}")
            snippet = (ab.get("snippet") or "").strip()
            if snippet:
                lang = (ab.get("snippet_language") or "text").strip()
                lines.append(f"\n```{lang}\n{snippet}\n```\n")

    # Scoring explainability + full list (kept collapsible to reduce noise).
    lines.append("\n<details><summary>Scoring details</summary>")
    rub = decision.get("rubric") or {}
    lines.append(f"- Weights: {json.dumps(rub.get('weights') or {}, ensure_ascii=False)}")
    lines.append(f"- Levels: {json.dumps(rub.get('levels') or {}, ensure_ascii=False)}")
    lines.append("\nAll findings (score → what it is):\n")
    items = decision.get("items") or []
    for it in items[:40]:
        lines.append(f"- [{it.get('level')}] {it.get('score')} — {it.get('category')}: {it.get('failure')}")
    if len(items) > 40:
        lines.append(f"- … ({len(items) - 40} more)")
    lines.append("</details>")

    return "\n".join(lines) + "\n"
