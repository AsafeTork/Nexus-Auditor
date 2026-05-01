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


def _ecom_problem_copy(f: Finding) -> str:
    text = " ".join([f.category or "", f.failure or "", f.explanation or "", f.solution or ""]).lower()
    mappings = [
        (("checkout", "payment", "gateway"), "Checkout pode falhar e fazer pedidos não serem concluídos."),
        (("cookie", "session", "login", "auth", "jwt"), "Sessão da compra pode quebrar e interromper o checkout."),
        (("ssl", "tls", "https", "certificate"), "Sinal de segurança fraco pode reduzir confiança antes do pagamento."),
        (("xss", "script", "input", "form"), "Campos e scripts inseguros podem prejudicar confiança e conversão."),
        (("redirect", "open redirect"), "Redirecionamento inseguro pode tirar o cliente do fluxo de compra."),
        (("cors", "api"), "Integração exposta pode quebrar catálogo, carrinho ou checkout."),
        (("headers", "hsts", "csp", "referrer-policy"), "Proteção fraca do site pode afetar confiança na compra."),
        (("sqli", "sql injection", "database"), "Falha crítica pode interromper a operação e afetar vendas."),
    ]
    for keywords, message in mappings:
        if any(k in text for k in keywords):
            return message
    failure = (f.failure or "").strip()
    if failure:
        failure = failure[:110].rstrip(".")
        return f"{failure} pode afetar a jornada de compra."
    return "Problema detectado pode afetar a jornada de compra."


def _ecom_impact_copy(f: Finding, level: str) -> str:
    level = str(level or "").upper()
    text = " ".join([f.category or "", f.failure or "", f.explanation or "", f.loss or ""]).lower()
    if any(k in text for k in ("checkout", "payment", "gateway", "cart", "session", "cookie")):
        return "Pode derrubar vendas ao quebrar checkout, reduzir conversão e enfraquecer a confiança do comprador."
    if any(k in text for k in ("ssl", "tls", "https", "header", "hsts", "csp")):
        return "Pode reduzir confiança na compra, afetar conversão e criar atrito antes do checkout."
    if any(k in text for k in ("xss", "csrf", "redirect", "cors", "api", "sqli")):
        return "Pode afetar vendas ao comprometer checkout, conversão e confiança em páginas críticas da loja."
    if level in ("CRITICAL", "HIGH"):
        return "Pode afetar vendas, checkout, conversão e confiança se continuar aberto."
    if level == "MEDIUM":
        return "Pode reduzir conversão, gerar atrito no checkout e desgastar a confiança ao longo do tempo."
    return "Vale corrigir para proteger vendas, conversão e confiança da loja."


def _ecom_money_at_risk_copy(f: Finding, level: str, score: int) -> str:
    level = str(level or "").upper()
    score = int(score or 0)
    text = " ".join([f.category or "", f.failure or "", f.loss or ""]).lower()
    if any(k in text for k in ("checkout", "payment", "cart", "session")):
        return "Dinheiro em risco: perda direta de vendas e aumento de abandono no checkout."
    if any(k in text for k in ("product", "catalog", "search", "script", "performance")):
        return "Dinheiro em risco: queda de conversão em páginas de produto e menos pedidos concluídos."
    if any(k in text for k in ("ssl", "tls", "https", "headers", "hsts", "csp", "redirect")):
        return "Dinheiro em risco: perda de conversão por menor confiança no momento de comprar."
    if level == "CRITICAL" or score >= 85:
        return "Dinheiro em risco: perda direta de receita se clientes abandonarem a compra."
    if level == "HIGH" or score >= 70:
        return "Dinheiro em risco: queda perceptível de conversão e pedidos concluídos."
    if level == "MEDIUM":
        return "Dinheiro em risco: erosão gradual de conversão, confiança e performance comercial."
    return "Dinheiro em risco: impacto indireto em confiança e conversão se o problema persistir."


def _ecom_urgency_copy(level: str) -> str:
    level = str(level or "").upper()
    if level == "CRITICAL":
        return "Agir agora"
    if level == "HIGH":
        return "Próximas 24h"
    if level == "MEDIUM":
        return "Esta semana"
    return "Monitorar"


def _financial_severity_view(level: str, score: int) -> Dict:
    level = str(level or "").upper()
    score = int(score or 0)
    if level == "CRITICAL" or score >= 85:
        return {
            "label": "Checkout em risco / perda imediata",
            "short_label": "Perda imediata",
            "summary": "Esse problema pode interromper compras e causar perda imediata de receita.",
        }
    if level == "HIGH" or score >= 70:
        return {
            "label": "Perda direta de receita",
            "short_label": "Receita em risco",
            "summary": "Esse problema pode derrubar vendas já nas próximas sessões de compra.",
        }
    if level == "MEDIUM" or score >= 45:
        return {
            "label": "Risco de perda de vendas",
            "short_label": "Vendas em risco",
            "summary": "Esse problema pode reduzir conversão e fazer a loja perder pedidos ao longo do tempo.",
        }
    return {
        "label": "Impacto baixo em conversão",
        "short_label": "Conversão sob atenção",
        "summary": "Esse problema tende a ter impacto menor, mas ainda pode corroer conversão e confiança se persistir.",
    }


def _ecom_action_copy(f: Finding, fallback_rec: str) -> str:
    text = " ".join([f.category or "", f.failure or "", f.solution or "", f.explanation or ""]).lower()
    mappings = [
        (("checkout", "payment", "gateway"), "Validar o fluxo de checkout, pagamento e retorno do pedido antes de liberar tráfego."),
        (("cookie", "session", "login", "auth", "jwt"), "Reforçar sessão, cookies e login para evitar quebra do carrinho e do checkout."),
        (("ssl", "tls", "https", "certificate"), "Corrigir HTTPS e sinais de confiança para não perder compradores antes do pagamento."),
        (("headers", "hsts", "csp", "referrer-policy"), "Ativar proteção básica da loja para aumentar confiança e reduzir risco no checkout."),
        (("xss", "script", "input", "form"), "Proteger formulários, scripts e entradas para preservar conversão e confiança."),
        (("redirect", "open redirect"), "Remover redirecionamentos inseguros que podem tirar clientes do fluxo de compra."),
        (("cors", "api"), "Restringir integrações expostas para estabilizar catálogo, carrinho e checkout."),
        (("sqli", "sql injection", "database"), "Corrigir a falha crítica e adicionar teste de regressão para proteger operação e vendas."),
    ]
    for keywords, copy in mappings:
        if any(k in text for k in keywords):
            return copy
    rec = (fallback_rec or f.solution or "").strip()
    if rec:
        return rec[:180]
    return "Corrigir este ponto primeiro para proteger vendas, checkout, conversão e confiança."


def build_ecommerce_finding_view(f: Finding, *, level: str, score: int, confidence: float | None, recommendation: str) -> Dict:
    fin = _financial_severity_view(level, score)
    return {
        "severidade_financeira": fin["label"],
        "severidade_financeira_curta": fin["short_label"],
        "resumo_financeiro": fin["summary"],
        "problema": _ecom_problem_copy(f),
        "impacto": _ecom_impact_copy(f, level),
        "dinheiro_em_risco": _ecom_money_at_risk_copy(f, level, score),
        "urgencia": _ecom_urgency_copy(level),
        "acao_recomendada": _ecom_action_copy(f, recommendation),
        "confianca": None if confidence is None else round(float(confidence), 3),
    }


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
        "loss": f.loss,
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

        ecom_view = build_ecommerce_finding_view(
            f,
            level=str(it.get("level") or ""),
            score=int(it.get("score") or 0),
            confidence=it.get("confidence"),
            recommendation=str(it.get("recommendation") or ""),
        )
        it["ecommerce"] = ecom_view

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
        "financial_levels": {
            "LOW": "impacto baixo em conversão",
            "MEDIUM": "risco de perda de vendas",
            "HIGH": "perda direta de receita",
            "CRITICAL": "checkout em risco / perda imediata",
        },
        "levels": {"CRITICAL": ">=85", "HIGH": "70-84", "MEDIUM": "45-69", "LOW": "<45"},
        "notes": [
            "A saída para o usuário converte prioridade técnica em impacto financeiro.",
            "Problemas com mais chance de afetar checkout, conversão e confiança sobem na prioridade.",
            "Problemas recorrentes recebem mais urgência porque podem continuar queimando receita.",
            "Confiança histórica ajusta a ordem para destacar o que tende a custar mais dinheiro se permanecer aberto.",
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
        ev = t.get("ecommerce") or {}
        sev = (ev.get("severidade_financeira") or "").strip()
        header = sev if sev else (ev.get('problema') or t.get('failure') or "")
        lines.append(f"{i}. {header}")
        problem = (ev.get("problema") or t.get("failure") or "").strip()
        if problem:
            lines.append(f"   - Problema: {problem}")
        impact = (ev.get("impacto") or "").strip()
        if impact:
            lines.append(f"   - Impacto: {impact}")
        money = (ev.get("dinheiro_em_risco") or "").strip()
        if money:
            lines.append(f"   - Dinheiro em risco: {money}")
        urg = (ev.get("urgencia") or "").strip()
        if urg:
            lines.append(f"   - Urgência: {urg}")
        conf = t.get("confidence", None)
        if conf is not None:
            lines.append(f"   - Confidence: {conf} (learned from past outcomes)")
        rec = (ev.get("acao_recomendada") or t.get("recommendation") or "").strip()
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
    lines.append(f"- Impacto financeiro: {json.dumps(rub.get('financial_levels') or {}, ensure_ascii=False)}")
    lines.append("\nAll findings (score → what it is):\n")
    items = decision.get("items") or []
    for it in items[:40]:
        ev = it.get("ecommerce") or {}
        lines.append(f"- {ev.get('severidade_financeira') or ev.get('problema') or it.get('failure')}")
    if len(items) > 40:
        lines.append(f"- … ({len(items) - 40} more)")
    lines.append("</details>")

    return "\n".join(lines) + "\n"
