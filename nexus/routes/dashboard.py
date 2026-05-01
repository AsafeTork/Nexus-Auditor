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
        return "24h (estim.)"
    hours = round(seconds / 3600.0, 1)
    if hours < 24:
        return f"{hours}h"
    days = round(hours / 24.0, 1)
    return f"{days}d"


def _risk_summary(status: str, active_issues: int) -> str:
    status = str(status or "").upper()
    if status == "CRITICAL":
        return "Seu site está vulnerável a problemas que podem afetar segurança, confiança e usuários."
    if status == "AT_RISK":
        return f"Seu site tem {active_issues} problema(s) ativo(s) que merecem atenção antes de impactar usuários ou reputação."
    if status == "PROTECTED":
        return "Seu site está protegido no momento, com risco visível sob controle e prioridades menores para acompanhar."
    return "Ainda não há dados suficientes para medir o risco real do site. Configure a base e rode a primeira auditoria."


def _sales_status_short(status: str) -> str:
    status = str(status or "").upper()
    return {
        "CRITICAL": "Perda imediata",
        "AT_RISK": "Vendas em risco",
        "PROTECTED": "Receita protegida",
        "NO_DATA": "Sem leitura real",
    }.get(status, "Sem leitura real")


def _sales_headline(status: str) -> str:
    status = str(status or "").upper()
    if status == "CRITICAL":
        return "Sim, sua loja pode estar perdendo vendas agora."
    if status == "AT_RISK":
        return "Sua loja tem sinais claros de perda de vendas."
    if status == "PROTECTED":
        return "Não há sinal forte de perda de vendas agora."
    return "Conecte sua loja para medir perda de vendas real."


def _sales_summary(status: str, active_issues: int) -> str:
    status = str(status or "").upper()
    if status == "CRITICAL":
        return "Problemas ativos podem interromper checkout, reduzir conversão e derrubar receita imediatamente."
    if status == "AT_RISK":
        return f"Existem {active_issues} pontos ativos que podem enfraquecer checkout, conversão e confiança do comprador."
    if status == "PROTECTED":
        return "Os sinais atuais indicam checkout mais estável, confiança preservada e menor risco de perda comercial."
    return "Ainda não há dados reais suficientes para mostrar o que ameaça suas vendas."


def _format_brl(value: int) -> str:
    value = int(max(0, int(value or 0)))
    text = f"{value:,.0f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {text}"


def _estimate_revenue_at_risk_monthly(*, open_findings: int, critical_sites: int, at_risk_sites: int, avg_risk_score: int, regression_count: int) -> int:
    base = (int(open_findings or 0) * 1200)
    base += (int(critical_sites or 0) * 9000)
    base += (int(at_risk_sites or 0) * 3500)
    base += max(0, int(avg_risk_score or 0) - 35) * 140
    base += int(regression_count or 0) * 1800
    return max(0, int(base))


def _financial_short_label(level: str) -> str:
    level = str(level or "").upper()
    if level == "CRITICAL":
        return "Perda imediata"
    if level == "HIGH":
        return "Receita em risco"
    if level == "MEDIUM":
        return "Vendas em risco"
    return "Conversão sob atenção"


def _impact_copy(level: str) -> str:
    level = str(level or "").upper()
    if level in ("CRITICAL", "HIGH"):
        return "Pode afetar segurança, confiança dos visitantes e operação do site se continuar aberto."
    if level == "MEDIUM":
        return "Aumenta o risco operacional e pode virar problema maior se não for tratado."
    return "Fortalece a proteção geral e reduz chance de problema futuro."


def _urgency_copy(level: str) -> str:
    level = str(level or "").upper()
    if level == "CRITICAL":
        return "Agir hoje"
    if level == "HIGH":
        return "Agir nas próximas 24h"
    if level == "MEDIUM":
        return "Agir nesta semana"
    return "Acompanhar"


def _owner_facing_fix(action_line: str, finding_key: str) -> str:
    text = f"{action_line or ''} {finding_key or ''}".lower()
    mappings = [
        (("header", "headers", "csp", "hsts", "referrer-policy"), "Ative os cabeçalhos de segurança no servidor ou CDN para bloquear ataques comuns."),
        (("cookie", "session", "login", "auth"), "Reforce cookies e sessões para evitar acesso indevido a contas de usuários."),
        (("ssl", "tls", "https", "certificate"), "Garanta HTTPS válido e redirecione todo acesso inseguro para conexão protegida."),
        (("redirect", "exposed", "exposure", "public"), "Feche páginas, portas ou caminhos expostos que não deveriam estar públicos."),
        (("dependency", "package", "library", "version"), "Atualize componentes desatualizados para remover falhas conhecidas."),
        (("form", "input", "xss", "script"), "Proteja formulários e entradas para impedir abuso no navegador do visitante."),
        (("admin", "panel", "dashboard", "secret"), "Restrinja ou esconda áreas sensíveis que não devem ficar acessíveis."),
    ]
    for keywords, copy in mappings:
        if any(k in text for k in keywords):
            return copy
    base = (action_line or "").strip()
    if base:
        return base[:160]
    return "Abra a tela de prioridades para ver a recomendação completa."


def _simplify_priority_title(action_line: str, finding_key: str) -> str:
    text = f"{action_line or ''} {finding_key or ''}".lower()
    mappings = [
        (("header", "headers", "csp", "hsts", "referrer-policy"), "Reforçar proteção básica do site"),
        (("cookie", "session", "login", "auth"), "Proteger sessões e acesso de usuários"),
        (("ssl", "tls", "https", "certificate"), "Garantir conexão segura para visitantes"),
        (("redirect", "exposed", "exposure", "public"), "Reduzir exposição pública do site"),
        (("dependency", "package", "library", "version"), "Atualizar componentes com risco"),
        (("form", "input", "xss", "script"), "Bloquear falhas que afetam usuários no navegador"),
        (("admin", "panel", "dashboard", "secret"), "Fechar acesso sensível exposto"),
    ]
    for keywords, title in mappings:
        if any(k in text for k in keywords):
            return title

    clean = (action_line or finding_key or "").replace("|", " ").replace("_", " ").strip(" -:")
    clean = clean.split(".")[0].strip()
    if clean:
        return clean[:72]
    return "Corrigir falha prioritária no site"


def _build_priority_tasks(cards: list[dict], *, provider_ready: bool, has_sites: bool, has_audits: bool, limit: int = 4) -> list[dict]:
    tasks: list[dict] = []
    seen = set()
    for c in cards:
        site = (c or {}).get("site") or {}
        site_name = str(site.get("name") or site.get("base_url") or "Site")
        for it in ((c or {}).get("top3") or []):
            title = str(it.get("problem") or "").strip() or _simplify_priority_title(str(it.get("action_line") or ""), str(it.get("finding_key") or ""))
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
                    "impact": str(it.get("impact") or "").strip() or _impact_copy(level),
                    "money_at_risk": str(it.get("money_at_risk") or "").strip() or "Dinheiro em risco: perda de conversão, confiança e pedidos se este problema continuar aberto.",
                    "how_to_fix": str(it.get("action_recommended") or "").strip() or _owner_facing_fix(str(it.get("action_line") or ""), str(it.get("finding_key") or "")),
                    "urgency": str(it.get("urgency") or "").strip() or _urgency_copy(level),
                    "level": level or "MEDIUM",
                    "score": score,
                    "confidence": it.get("confidence"),
                    "cta_kind": "priorities",
                }
            )

    tasks = sorted(
        tasks,
        key=lambda item: (
            {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(str(item.get("level") or "").upper(), 0),
            float(item.get("score") or 0),
        ),
        reverse=True,
    )
    if tasks:
        return tasks[:limit]

    if not provider_ready:
        return [
            {
                "site_name": "Base do sistema",
                "title": "Conectar o motor de análise",
                "financial_label": "Receita sem visibilidade",
                "financial_short_label": "Receita sem leitura",
                "impact": "Sem isso o produto não consegue mostrar o que ameaça vendas, checkout, conversão e confiança.",
                "money_at_risk": "Dinheiro em risco: perda de clareza sobre onde a loja pode estar perdendo pedidos.",
                "how_to_fix": "Abra Configurações, valide a chave do provedor e selecione um modelo funcional.",
                "urgency": "Agir agora",
                "level": "HIGH",
                "score": 100,
                "cta_kind": "provider",
            }
        ]
    if not has_sites:
        return [
            {
                "site_name": "Primeiro alvo",
                "title": "Adicionar o site principal",
                "financial_label": "Vendas sem proteção",
                "financial_short_label": "Sem leitura de vendas",
                "impact": "Sem um site cadastrado o sistema não consegue avaliar vendas, checkout, conversão e confiança.",
                "money_at_risk": "Dinheiro em risco: você continua sem visibilidade sobre os pontos que podem estar derrubando receita.",
                "how_to_fix": "Cadastre a URL principal do site para habilitar análises e histórico.",
                "urgency": "Agir agora",
                "level": "HIGH",
                "score": 90,
                "cta_kind": "create_site",
            }
        ]
    if not has_audits:
        return [
            {
                "site_name": "Primeira leitura",
                "title": "Executar a primeira auditoria",
                "financial_label": "Receita sem diagnóstico",
                "financial_short_label": "Sem diagnóstico",
                "impact": "É ela que revela os pontos que podem afetar vendas, checkout, conversão e confiança.",
                "money_at_risk": "Dinheiro em risco: sem auditoria você não enxerga onde a loja pode estar perdendo pedidos agora.",
                "how_to_fix": "Inicie um fluxo completo em um dos sites cadastrados para popular o painel.",
                "urgency": "Agir agora",
                "level": "HIGH",
                "score": 80,
                "cta_kind": "sites",
            }
        ]
    return []


def _detect_demo_site_type(sites: list[Site], audits: list) -> str:
    texts: list[str] = []
    for s in sites or []:
        texts.append(str(getattr(s, "base_url", "") or ""))
        texts.append(str(getattr(s, "name", "") or ""))
    for a in audits or []:
        texts.append(str(getattr(a, "target_domain", "") or ""))
    haystack = " ".join(texts).lower()
    if any(token in haystack for token in ("shop", "store", "cart")):
        return "ecommerce"
    if any(token in haystack for token in ("app", "dashboard", "api")):
        return "saas"
    return "institucional"


def _build_demo_dashboard_state(site_type: str) -> dict:
    site_type = (site_type or "institucional").strip().lower()
    demo_map = {
        "ecommerce": {
            "segment_label": "E-commerce",
            "site_name_1": "Loja principal",
            "site_name_2": "Catálogo e checkout",
            "base_url_1": "https://shop-demo.com",
            "base_url_2": "https://store-demo.com",
            "summary": "Exemplo de leitura para e-commerce: o checkout pode falhar, a confiança do comprador pode cair e a sessão pode quebrar a compra.",
            "impact_anchor": "Perda direta de receita",
            "immediate_action": "Corrija primeiro o checkout, depois o sinal de confiança e em seguida a sessão da compra.",
            "findings": [
                {"title": "Checkout com falha pode travar pedidos", "impact": "Pode interromper a compra no momento de pagamento e causar perda imediata de vendas.", "urgency": "Agir agora", "confidence": 0.93, "status": "open", "level": "CRITICAL", "how_to_fix": "Validar o fluxo de checkout, pagamento e retorno do pedido antes de liberar mais tráfego."},
                {"title": "Sinal de confiança fraco pode derrubar conversão", "impact": "Pode fazer compradores abandonarem a compra por desconfiança no momento mais sensível.", "urgency": "Agir agora", "confidence": 0.89, "status": "open", "level": "CRITICAL", "how_to_fix": "Reforçar HTTPS, sinais visuais de confiança e proteção nas páginas de produto e checkout."},
                {"title": "Problema de sessão pode quebrar o carrinho", "impact": "Pode apagar a continuidade da compra, elevar abandono e reduzir pedidos concluídos.", "urgency": "Próximas 24h", "confidence": 0.86, "status": "open", "level": "CRITICAL", "how_to_fix": "Reforçar cookies, expiração de sessão e recuperação do carrinho no backend."},
                {"title": "Catálogo lento pode reduzir adição ao carrinho", "impact": "Pode diminuir descoberta de produto e conversão ao longo da navegação.", "urgency": "Esta semana", "confidence": 0.73, "status": "open", "level": "MEDIUM", "how_to_fix": "Ajustar scripts e carregamento de páginas de produto para preservar velocidade comercial."},
                {"title": "Tag externa sem controle pode atrasar páginas de venda", "impact": "Pode reduzir conversão ao atrasar páginas de produto, vitrine e carrinho.", "urgency": "Esta semana", "confidence": 0.69, "status": "open", "level": "MEDIUM", "how_to_fix": "Limitar scripts externos e priorizar o carregamento do que ajuda a vender."},
                {"title": "Redirecionamento inseguro no checkout resolvido", "impact": "Uma falha que poderia afetar compra já foi corrigida recentemente.", "urgency": "Resolvido", "confidence": 0.78, "status": "resolved", "level": "LOW", "how_to_fix": "Correção confirmada no fluxo de pagamento e navegação."},
            ],
            "card_actions": [
                ("Blindar o checkout para evitar perda imediata", "demo|checkout-fail", "CRITICAL", 98, 0.93),
                ("Reforçar confiança para proteger conversão", "demo|trust-issue", "CRITICAL", 93, 0.89),
                ("Proteger sessão e continuidade do carrinho", "demo|cart-session", "CRITICAL", 89, 0.86),
                ("Estabilizar busca e catálogo para preservar conversão", "demo|catalog-stability", "MEDIUM", 58, 0.73),
            ],
        },
        "saas": {
            "segment_label": "SaaS",
            "site_name_1": "Aplicação principal",
            "site_name_2": "Área logada",
            "base_url_1": "https://app-demo.com",
            "base_url_2": "https://dashboard-demo.com",
            "summary": "Exemplo de risco para SaaS: falhas visíveis podem quebrar fluxo logado, gerar suporte e elevar churn.",
            "impact_anchor": "Perda de usuários ativos",
            "immediate_action": "Veja abaixo como a plataforma priorizaria riscos que ameaçam retenção e uso diário.",
            "findings": [
                {"title": "Erro pode quebrar fluxo do usuário logado", "impact": "Perda de usuários ativos ao interromper a experiência principal da conta.", "urgency": "Agir agora", "confidence": 0.90, "status": "open", "level": "CRITICAL", "how_to_fix": "Mapeie erros do fluxo logado e proteja as rotas essenciais com testes e fallback."},
                {"title": "Instabilidade pode aumentar churn", "impact": "Quedas e respostas inconsistentes prejudicam retenção e confiança no produto.", "urgency": "Agir agora", "confidence": 0.87, "status": "open", "level": "CRITICAL", "how_to_fix": "Aumente resiliência nas rotas críticas e trate erros de forma previsível para o usuário."},
                {"title": "API exposta pode afetar sessão e dados", "impact": "Pode comprometer experiência logada e criar risco operacional imediato.", "urgency": "24h", "confidence": 0.83, "status": "open", "level": "CRITICAL", "how_to_fix": "Reforce autenticação, rate limit e validação nas rotas de API mais sensíveis."},
                {"title": "Dependência instável pode gerar lentidão recorrente", "impact": "Afeta produtividade e aumenta atrito em uso diário.", "urgency": "Esta semana", "confidence": 0.72, "status": "open", "level": "MEDIUM", "how_to_fix": "Atualize dependências com histórico de falha e revise gargalos do backend."},
                {"title": "Evento assíncrono sem proteção suficiente", "impact": "Pode criar erros silenciosos e desgaste na operação do suporte.", "urgency": "Esta semana", "confidence": 0.68, "status": "open", "level": "MEDIUM", "how_to_fix": "Aplique fila, retry controlado e observabilidade nos eventos principais."},
                {"title": "Sessão insegura em rota secundária resolvida", "impact": "Um ponto de atrito e risco para usuários já foi removido.", "urgency": "Resolvido", "confidence": 0.77, "status": "resolved", "level": "LOW", "how_to_fix": "Correção verificada na área logada."},
            ],
            "card_actions": [
                ("Blindar o fluxo logado para evitar quebra de uso", "demo|logged-flow", "CRITICAL", 96, 0.90),
                ("Reduzir instabilidade que pode elevar churn", "demo|churn-instability", "CRITICAL", 92, 0.87),
                ("Fechar exposição crítica da API", "demo|api-exposed", "CRITICAL", 88, 0.83),
                ("Estabilizar eventos e dependências críticas", "demo|async-stability", "MEDIUM", 58, 0.72),
            ],
        },
        "institucional": {
            "segment_label": "Institucional",
            "site_name_1": "Site principal",
            "site_name_2": "Página de contato",
            "base_url_1": "https://institucional-demo.com",
            "base_url_2": "https://www.institucional-demo.com",
            "summary": "Exemplo de risco para site institucional: falhas visíveis podem afetar credibilidade, contato comercial e confiança.",
            "impact_anchor": "Perda de confiança",
            "immediate_action": "Veja abaixo como a plataforma destacaria riscos que afetam reputação e captação de contatos.",
            "findings": [
                {"title": "Formulário pode estar vulnerável a spam", "impact": "Perda de confiança e desgaste operacional em canais de contato.", "urgency": "Agir agora", "confidence": 0.89, "status": "open", "level": "CRITICAL", "how_to_fix": "Aplique validação, proteção antispam e checagem no backend do formulário."},
                {"title": "Falhas podem afetar credibilidade", "impact": "Erros públicos reduzem confiança de visitantes, leads e parceiros.", "urgency": "Agir agora", "confidence": 0.85, "status": "open", "level": "CRITICAL", "how_to_fix": "Corrija erros visíveis e fortaleça proteção básica para evitar sinais públicos de risco."},
                {"title": "Script externo pode comprometer páginas principais", "impact": "Pode degradar páginas institucionais e enfraquecer a percepção de profissionalismo.", "urgency": "24h", "confidence": 0.81, "status": "open", "level": "CRITICAL", "how_to_fix": "Reduza dependências externas nas páginas mais visitadas e proteja carregamento crítico."},
                {"title": "Upload de currículo pode estar exposto", "impact": "Pode criar abuso ou envio indevido em canais secundários.", "urgency": "Esta semana", "confidence": 0.71, "status": "open", "level": "MEDIUM", "how_to_fix": "Aplique restrições de tipo, tamanho e validação forte em uploads."},
                {"title": "Página de contato com proteção insuficiente", "impact": "Pode aumentar ruído operacional e prejudicar a comunicação real.", "urgency": "Esta semana", "confidence": 0.68, "status": "open", "level": "MEDIUM", "how_to_fix": "Adicione rate limit e bloqueios simples para abuso repetitivo."},
                {"title": "Redirecionamento inseguro em página institucional resolvido", "impact": "Um ponto que poderia afetar confiança já foi eliminado.", "urgency": "Resolvido", "confidence": 0.76, "status": "resolved", "level": "LOW", "how_to_fix": "Correção já validada na navegação principal."},
            ],
            "card_actions": [
                ("Proteger formulários e canais de contato", "demo|contact-form", "CRITICAL", 96, 0.89),
                ("Corrigir falhas que afetam credibilidade pública", "demo|credibility", "CRITICAL", 92, 0.85),
                ("Reduzir risco de scripts em páginas principais", "demo|public-scripts", "CRITICAL", 88, 0.81),
                ("Blindar páginas secundárias contra abuso", "demo|secondary-pages", "MEDIUM", 58, 0.71),
            ],
        },
    }
    scenario = demo_map.get(site_type) or demo_map["institucional"]
    demo_findings = scenario["findings"]

    open_tasks = []
    for item in demo_findings:
        if item["status"] != "open":
            continue
        open_tasks.append(
            {
                "site_name": scenario["base_url_1"].replace("https://", ""),
                "title": item["title"],
                "impact": item["impact"],
                "money_at_risk": (
                    "Dinheiro em risco: até R$ 8.400/mês se o checkout continuar falhando."
                    if "checkout" in item["title"].lower()
                    else "Dinheiro em risco: até R$ 5.200/mês em conversão perdida por queda de confiança."
                    if "confiança" in item["title"].lower() or "confianca" in item["title"].lower()
                    else "Dinheiro em risco: até R$ 4.700/mês em abandono de carrinho por sessão instável."
                    if "sessão" in item["title"].lower() or "sessao" in item["title"].lower()
                    else f"Dinheiro em risco: {scenario['impact_anchor']}."
                ),
                "how_to_fix": item["how_to_fix"],
                "urgency": item["urgency"],
                "financial_label": _financial_short_label(item["level"]),
                "financial_short_label": _financial_short_label(item["level"]),
                "level": item["level"],
                "score": 96 if item["level"] == "CRITICAL" else 58,
                "confidence": item["confidence"],
                "status": item["status"],
                "cta_kind": "create_site",
            }
        )

    demo_cards = [
        {
            "site": {"id": "demo-site-1", "name": scenario["site_name_1"], "base_url": scenario["base_url_1"]},
            "last_run": {"id": "", "created_utc": "", "status": "done"},
            "overall": {"status": "AT_RISK", "confidence": 0.84, "coverage_quality": "MEDIUM", "strictness": 68, "instability_score": 24},
            "top3": [
                {"finding_key": scenario["card_actions"][0][1], "score": scenario["card_actions"][0][3], "level": scenario["card_actions"][0][2], "confidence": scenario["card_actions"][0][4], "action_line": scenario["card_actions"][0][0], "problem": scenario["findings"][0]["title"], "impact": scenario["findings"][0]["impact"], "financial_label": _financial_short_label(scenario["card_actions"][0][2]), "urgency": scenario["findings"][0]["urgency"]},
                {"finding_key": scenario["card_actions"][1][1], "score": scenario["card_actions"][1][3], "level": scenario["card_actions"][1][2], "confidence": scenario["card_actions"][1][4], "action_line": scenario["card_actions"][1][0], "problem": scenario["findings"][1]["title"], "impact": scenario["findings"][1]["impact"], "financial_label": _financial_short_label(scenario["card_actions"][1][2]), "urgency": scenario["findings"][1]["urgency"]},
            ],
            "value": {"open_findings": 3, "resolved_findings": 1, "fix_success_rate": 72, "avg_time_to_fix_s": 8040, "regression_count": 1},
            "flags": {"has_decision_json": True, "has_verification_json": True},
        },
        {
            "site": {"id": "demo-site-2", "name": scenario["site_name_2"], "base_url": scenario["base_url_2"]},
            "last_run": {"id": "", "created_utc": "", "status": "done"},
            "overall": {"status": "AT_RISK", "confidence": 0.71, "coverage_quality": "MEDIUM", "strictness": 54, "instability_score": 12},
            "top3": [
                {"finding_key": scenario["card_actions"][2][1], "score": scenario["card_actions"][2][3], "level": scenario["card_actions"][2][2], "confidence": scenario["card_actions"][2][4], "action_line": scenario["card_actions"][2][0], "problem": scenario["findings"][2]["title"], "impact": scenario["findings"][2]["impact"], "financial_label": _financial_short_label(scenario["card_actions"][2][2]), "urgency": scenario["findings"][2]["urgency"]},
                {"finding_key": scenario["card_actions"][3][1], "score": scenario["card_actions"][3][3], "level": scenario["card_actions"][3][2], "confidence": scenario["card_actions"][3][4], "action_line": scenario["card_actions"][3][0], "problem": scenario["findings"][3]["title"], "impact": scenario["findings"][3]["impact"], "financial_label": _financial_short_label(scenario["card_actions"][3][2]), "urgency": scenario["findings"][3]["urgency"]},
            ],
            "value": {"open_findings": 2, "resolved_findings": 0, "fix_success_rate": 72, "avg_time_to_fix_s": 8040, "regression_count": 0},
            "flags": {"has_decision_json": True, "has_verification_json": True},
        },
    ]

    return {
        "demo_mode": True,
        "demo_site_type": site_type,
        "demo_segment_label": scenario["segment_label"],
        "demo_note": f"Exemplo de análise automática para {scenario['segment_label'].lower()} — conecte seu site para dados reais.",
        "priority_tasks": open_tasks,
        "featured_cards": demo_cards,
        "priority_total": len(open_tasks),
        "risk_overview": {
            "status": "AT_RISK",
            "status_label": "AT RISK",
            "sales_status_short": "Vendas em risco",
            "sales_headline": "Sim, esta loja pode estar perdendo vendas agora.",
            "summary": scenario["summary"],
            "active_issues": 5,
            "avg_risk_score": 68,
            "avg_risk_label": "High",
            "risk_bar_pct": 68,
            "critical_sites": 1,
            "at_risk_sites": 1,
            "protected_sites": 0,
            "monitored_sites": 1,
            "immediate_action": scenario["findings"][0]["title"],
            "immediate_action_urgency": scenario["findings"][0]["urgency"],
        },
        "proof_metrics": {
            "resolved_recent": 1,
            "avg_fix_time_label": "2h 14min",
            "risk_drop_pct": 72,
            "sites_with_action": 2,
            "fix_success_rate": 72,
            "regression_count": 1,
            "estimated_loss_monthly": 18400,
            "estimated_loss_label": f"{_format_brl(18400)}/mês",
            "estimated_loss_detail": "Estimativa de quanto a loja pode deixar na mesa se os problemas de checkout e conversão continuarem abertos.",
        },
    }


@bp.get("/")
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
    estimated_loss_monthly = _estimate_revenue_at_risk_monthly(
        open_findings=total_open_findings,
        critical_sites=critical_sites,
        at_risk_sites=at_risk_sites,
        avg_risk_score=avg_risk_score,
        regression_count=sum(int((c.get("value") or {}).get("regression_count") or 0) for c in sorted_cards),
    )
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
        "sales_headline": _sales_headline(overall_status),
        "summary": _sales_summary(overall_status, total_open_findings),
        "active_issues": total_open_findings,
        "avg_risk_score": avg_risk_score,
        "avg_risk_label": _risk_label(avg_risk_score),
        "risk_bar_pct": max(4, min(100, avg_risk_score if avg_risk_score else 8)),
        "critical_sites": critical_sites,
        "at_risk_sites": at_risk_sites,
        "protected_sites": protected_sites,
        "monitored_sites": sites_count,
        "immediate_action": (
            str(next_task.get("title") or "Ataque a correção mais urgente abaixo agora.")
            if priority_tasks and overall_status in ("CRITICAL", "AT_RISK")
            else "Continue acompanhando para preservar a receita protegida."
            if overall_status == "PROTECTED"
            else "Conecte a loja para revelar onde você pode estar perdendo vendas."
        ),
        "immediate_action_urgency": str(next_task.get("urgency") or ("Agir agora" if overall_status in ("CRITICAL", "AT_RISK") else "Acompanhar")),
    }
    proof_metrics = {
        "resolved_recent": total_resolved_findings,
        "avg_fix_time_label": _format_duration(avg_fix_time_s),
        "risk_drop_pct": risk_drop_pct,
        "sites_with_action": len(visible_priorities),
        "fix_success_rate": round(sum(float((c.get("value") or {}).get("fix_success_rate") or 0) for c in sorted_cards) / max(1, len(sorted_cards))) if sorted_cards else 0,
        "regression_count": sum(int((c.get("value") or {}).get("regression_count") or 0) for c in sorted_cards),
        "estimated_loss_monthly": estimated_loss_monthly,
        "estimated_loss_label": f"{_format_brl(estimated_loss_monthly)}/mês" if estimated_loss_monthly > 0 else "Baixo no momento",
        "estimated_loss_detail": (
            "Estimativa baseada nos problemas ativos que podem travar checkout, reduzir conversão e enfraquecer confiança."
            if estimated_loss_monthly > 0
            else "Nenhum sinal forte de perda de receita apareceu nas leituras atuais."
        ),
    }

    has_real_monitoring_runs = any(str(((c.get("last_run") or {}).get("id") or "")).strip() for c in sorted_cards)
    has_real_findings = (total_open_findings + total_resolved_findings) > 0
    has_real_priorities = any((c.get("top3") or []) for c in sorted_cards)
    demo_mode = not (has_real_monitoring_runs and has_real_findings and has_real_priorities)

    if demo_mode:
        detected_demo_site_type = _detect_demo_site_type(sites, audits)
        demo = _build_demo_dashboard_state(detected_demo_site_type)
        priority_tasks = demo["priority_tasks"]
        featured_cards = demo["featured_cards"]
        priority_total = int(demo["priority_total"])
        risk_overview = demo["risk_overview"]
        proof_metrics = demo["proof_metrics"]
        demo_note = demo["demo_note"]
        demo_segment_label = demo["demo_segment_label"]
    else:
        priority_total = len(visible_priorities)
        demo_note = ""
        demo_segment_label = ""

    return render_template(
        "dashboard/home.html",
        sites=sites,
        audits=audits,
        sub=sub,
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
        demo_note=demo_note,
        demo_segment_label=demo_segment_label,
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
