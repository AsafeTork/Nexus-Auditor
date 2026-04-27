from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import redis
from rq import Worker, Queue, Connection

from . import create_app, db
from .models import AuditEvent, AuditRun, Site
from .services.audit_engine import (
    MICRO_LAYERS,
    SYSTEM_PROMPT_DEFAULT,
    build_user_prompt,
    call_llm_non_stream,
    clean_html,
    estimate_ltv_loss_from_rows,
    fetch_url_html,
)


def run_audit_job(audit_id: str) -> None:
    """
    Execute a full audit run and persist outputs to DB.
    """
    app = create_app()
    with app.app_context():
        audit = AuditRun.query.filter_by(id=audit_id).first()
        if not audit:
            return
        audit.status = "running"
        audit.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        db.session.commit()

        def log(layer: str, level: str, msg: str) -> None:
            try:
                audit.logs = (audit.logs or "") + msg + "\n"
                db.session.add(AuditEvent(audit_run_id=audit.id, layer=layer, level=level, message=msg))
                audit.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                db.session.commit()
            except Exception:
                # Prevent cascading failures after an insert error.
                try:
                    db.session.rollback()
                except Exception:
                    pass

        def md(line: str) -> None:
            audit.markdown_text = (audit.markdown_text or "") + line + "\n"
            audit.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            db.session.commit()

        def csv(row: str) -> None:
            audit.csv_text = (audit.csv_text or "") + row + "\n"
            audit.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            db.session.commit()

        site = Site.query.filter_by(id=audit.site_id).first()
        if not site:
            audit.status = "error"
            db.session.commit()
            return

        base_url_v1 = audit.provider_base_url_v1 or app.config["LLM_BASE_URL_V1"]
        api_key = app.config.get("LLM_API_KEY", "")
        model = audit.model or app.config["LLM_DEFAULT_MODEL"]
        system_prompt = SYSTEM_PROMPT_DEFAULT

        try:
            log("fetch", "INFO", f"Fetching HTML: {site.base_url}")
            fetch = fetch_url_html(site.base_url)
            cleaned = clean_html(fetch.html)
            host = (urlparse(fetch.url).hostname or "")
            audit.target_domain = host
            db.session.commit()
        except Exception as e:
            log("fetch", "ERROR", f"Falha ao baixar HTML: {type(e).__name__}: {e}")
            audit.status = "error"
            db.session.commit()
            return

        md("# AUDIT_NEXUS — Professional Edition")
        md(f"- Target: {site.base_url}")
        md(f"- Final URL: {fetch.url}")
        md(f"- Provider: {base_url_v1}")
        md(f"- Model: {model}")
        md("")

        rows: list[str] = []

        for i, layer in enumerate(MICRO_LAYERS, start=1):
            log(layer, "INFO", f"Iniciando {layer} ({i}/{len(MICRO_LAYERS)})...")
            md(f"## {layer}")

            # Layer 10 is computed from rows
            if layer.startswith("10."):
                lo, hi = estimate_ltv_loss_from_rows(rows)
                md("### Executive Financial Summary (LTV Loss)")
                md(f"- Estimativa heurística baseada em prioridades do CSV.")
                md(f"- **LTV Loss estimado: USD {lo} – {hi}**")
                md("")
                fin = f"Financeiro;Executive LTV Loss (heurístico);Derivado do CSV desta auditoria;Faixa estimada por severidade/prioridade;USD {lo}–{hi};Ajustar com métricas reais;Média;Baixa"
                csv(fin)
                rows.append(fin)
                continue

            prompt = build_user_prompt(layer, fetch, cleaned)
            try:
                # Non-stream mode is more reliable in hosted environments.
                content = call_llm_non_stream(
                    base_url_v1=base_url_v1,
                    api_key=api_key,
                    model=model,
                    temperature=0.2,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    timeout_s=240,
                )
                if not content.strip():
                    log(layer, "ERROR", "Resposta vazia do provedor LLM.")
                    continue

                # Parse sections: ---REPORT--- ... ---CSV---
                report = ""
                csv_block = ""
                if "---REPORT---" in content:
                    content2 = content.split("---REPORT---", 1)[1]
                else:
                    content2 = content
                if "---CSV---" in content2:
                    report, csv_block = content2.split("---CSV---", 1)
                else:
                    report = content2

                for ln in report.splitlines():
                    if ln.strip():
                        md(ln)

                for ln in csv_block.splitlines():
                    row = ln.strip("\r").strip()
                    if not row:
                        continue
                    if row.lower().startswith("categoria;"):
                        continue
                    if row.count(";") >= 6:
                        csv(row)
                        rows.append(row)
            except Exception as e:
                log(layer, "ERROR", f"Falha no provedor LLM: {type(e).__name__}: {e}")
                # Continue to next layer; report remains useful even if one layer fails.
                continue

        audit.status = "done" if audit.status != "error" else "error"
        db.session.commit()


def main() -> None:
    app = create_app()
    with app.app_context():
        redis_url = app.config["REDIS_URL"]
    conn = redis.from_url(redis_url)
    with Connection(conn):
        worker = Worker([Queue("audits")])
        worker.work()


if __name__ == "__main__":
    main()
