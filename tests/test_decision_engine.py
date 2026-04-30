from nexus.services.decision_engine import build_decision_report, parse_csv_findings


def test_decision_engine_scores_and_levels():
    csv_text = """Categoria;Falha;Prova Técnica;Explicação;Prejuízo Estimado;Solução;Prioridade;Complexity
Segurança;SQL Injection risk;param=id;unsafe query;USD 1000–5000;Use parameterized queries;Alta;Média
Infra;Missing HSTS;header absent;downgrade risk;USD 200–800;Add Strict-Transport-Security;Média;Baixa
"""
    findings = parse_csv_findings(csv_text)
    decision = build_decision_report(findings, recurrence_map={f.key: 1 for f in findings}, top_n=3)
    assert "items" in decision and len(decision["items"]) == 2
    for it in decision["items"]:
        assert 0 <= int(it["score"]) <= 100
        assert it["level"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert it["recommendation"]
