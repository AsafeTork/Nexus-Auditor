from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import redis
from rq import Worker, Queue, Connection
from rq.exceptions import NoSuchJobError

from . import create_app, db
from .models import AuditEvent, AuditRun, Site, Organization
from .services.audit_engine import (
    MICRO_LAYERS,
    SYSTEM_PROMPT_DEFAULT,
    build_user_prompt,
    call_llm_non_stream,
    clean_html,
    estimate_ltv_loss_from_rows,
    fetch_url_html,
    stream_llm_events,
    stream_llm_text,
)
from .services.ui_review import read_text_files


def _ui_key(org_id: str, run_id: str) -> str:
    return f"ui_lab:{org_id}:{run_id}"


def _ui_index_key(org_id: str) -> str:
    return f"ui_lab:index:{org_id}"


def run_ui_lab_job(run_id: str, org_id: str, mode: str, payload: dict) -> None:
    """
    Execute UI Lab review in background and store status/logs/result in Redis.
    """
    app = create_app()
    with app.app_context():
        conn = redis.from_url(app.config["REDIS_URL"])
        key = _ui_key(org_id, run_id)

        def append_log(msg: str):
            try:
                conn.hset(key, mapping={"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                conn.hincrby(key, "log_len", len(msg) + 1)
                conn.append(key + ":logs", msg + "\n")
            except Exception:
                pass

        try:
            conn.hset(
                key,
                mapping={
                    "status": "running",
                    "mode": mode,
                    "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        except Exception:
            pass

        append_log(f"[ui-lab] start mode={mode}")

        # Build context
        context = ""
        try:
            if mode in ("templates", "auto"):
                # AUTO = analyze the whole UI surface (all templates).
                root = os.path.abspath(os.path.join(os.path.dirname(__file__), "templates"))
                targets: list[str] = []
                for r, _dirs, files in os.walk(root):
                    for fn in files:
                        if not fn.endswith(".html"):
                            continue
                        if fn.startswith("_"):
                            continue
                        targets.append(os.path.join(r, fn))
                targets = sorted(set(targets))
                append_log(f"[ui-lab] lendo templates ({len(targets)} arquivos)…")

                # Keep per-file size bounded and also bound the whole context to avoid LLM failures.
                max_each = int(os.getenv("UI_LAB_MAX_CHARS_EACH", "12000"))
                max_total = int(os.getenv("UI_LAB_MAX_CONTEXT_CHARS", "180000"))
                blob = read_text_files([p for p in targets if os.path.exists(p)], max_chars_each=max_each)
                if len(blob) > max_total:
                    blob = blob[:max_total] + "\n\n/* ... contexto truncado por tamanho ... */\n"
                    append_log(f"[ui-lab] contexto truncado para {max_total} chars")
                context = blob
            elif mode == "url":
                import requests

                url = str(payload.get("url") or "").strip()
                append_log(f"[ui-lab] baixando HTML: {url}")
                r = requests.get(url, timeout=18, headers={"User-Agent": "NexusAuditor/1.0"})
                r.raise_for_status()
                html = r.text or ""
                if len(html) > 20000:
                    html = html[:20000] + "\n<!-- ... truncado ... -->"
                context = f"URL: {url}\n\nHTML:\n{html}"
            elif mode == "screenshot":
                meta = payload.get("meta") or {}
                notes = str(payload.get("notes") or "")
                context = f"Screenshot meta: {meta}\nObservações: {notes}\n"
            else:
                context = f"Modo desconhecido: {mode}\nPayload: {payload}"
        except Exception as e:
            append_log(f"[ui-lab] erro ao montar contexto: {type(e).__name__}: {e}")
            try:
                conn.hset(key, mapping={"status": "error", "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
            return

        goal = str(payload.get("goal") or "").strip() or "Deixar a UI mais premium, clara, consistente e com foco."

        # Call LLM (streaming -> UI updates while generating)
        try:
            # Prefer org-level defaults if not explicitly passed in payload.
            org = Organization.query.filter_by(id=org_id).first()
            base_url_v1 = str(
                payload.get("base_url_v1")
                or (getattr(org, "llm_base_url_v1", "") if org else "")
                or app.config.get("LLM_BASE_URL_V1", "")
            ).strip()
            model = str(
                payload.get("model")
                or (getattr(org, "llm_model", "") if org else "")
                or app.config.get("LLM_DEFAULT_MODEL", "deepseek-chat")
            ).strip()
            api_key = app.config.get("LLM_API_KEY", "")
            append_log(f"[ui-lab] chamando LLM (stream)… model={model}")

            acc = ""
            last_flush = time.time()
            # UI Lab v2: output a single PROMPT to feed back into SOLO (not a long report).
            system_prompt = (
                "Você é um Product Designer + Frontend Engineer senior. "
                "Sua saída NÃO é um relatório para humanos lerem. "
                "Sua saída deve ser um ÚNICO PROMPT pronto para colar no SOLO, para ele implementar mudanças no código. "
                "Requisitos: (1) foco/hierarquia/ritmo vertical, (2) espaçamento consistente, (3) responsivo mobile+desktop, "
                "(4) acessibilidade (aria/keyboard/focus/contraste), (5) não inventar arquivos que não existem. "
                "Formato obrigatório:\n"
                "1) TÍTULO: \"PROMPT PARA SOLO\"\n"
                "2) CONTEXTO (2-3 linhas)\n"
                "3) OBJETIVO (bullet)\n"
                "4) REGRAS (bullet)\n"
                "5) PLANO DE ALTERAÇÕES POR ARQUIVO (checklist, com paths reais)\n"
                "6) TRECHOS DE CÓDIGO (somente quando necessário, curtos)\n"
                "7) COMO VALIDAR (passos rápidos)\n"
                "Seja direto e acionável."
            )

            for delta in stream_llm_text(
                base_url_v1=base_url_v1,
                api_key=api_key,
                model=model,
                temperature=0.2,
                system_prompt=system_prompt,
                user_prompt=f"Objetivo: {goal}\n\nContexto:\n{context}\n",
                timeout_s=240,
            ):
                acc += delta
                if time.time() - last_flush > 1.0:
                    conn.set(key + ":result", acc, ex=60 * 60 * 24 * 7)
                    last_flush = time.time()

            if not acc.strip():
                raise RuntimeError("Resposta vazia do LLM.")
            conn.set(key + ":result", acc, ex=60 * 60 * 24 * 7)
            conn.hset(key, mapping={"status": "done"})
            append_log("[ui-lab] done")
        except Exception as e:
            append_log(f"[ui-lab] erro LLM: {type(e).__name__}: {e}")
            try:
                conn.hset(key, mapping={"status": "error", "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass


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

        # PERFORMANCE: avoid committing per line (very slow on hosted DB).
        log_buf: list[str] = []
        md_buf: list[str] = []
        csv_buf: list[str] = []
        events_buf: list[AuditEvent] = []
        last_flush = time.time()

        def flush(force: bool = False) -> None:
            nonlocal last_flush
            if not force and (time.time() - last_flush) < 0.8 and len(events_buf) < 30 and len(log_buf) < 30:
                return
            try:
                if log_buf:
                    audit.logs = (audit.logs or "") + "".join(log_buf)
                    # Keep logs bounded to limit DB growth.
                    if len(audit.logs) > 200_000:
                        audit.logs = audit.logs[-200_000:]
                    log_buf.clear()
                if md_buf:
                    audit.markdown_text = (audit.markdown_text or "") + "".join(md_buf)
                    md_buf.clear()
                if csv_buf:
                    audit.csv_text = (audit.csv_text or "") + "".join(csv_buf)
                    csv_buf.clear()
                if events_buf:
                    db.session.add_all(events_buf)
                    events_buf.clear()
                audit.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                db.session.commit()
                last_flush = time.time()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass

        def log(layer: str, level: str, msg: str) -> None:
            # Always keep logs readable (one line each)
            line = (msg or "").rstrip("\n") + "\n"
            log_buf.append(line)
            events_buf.append(AuditEvent(audit_run_id=audit.id, layer=layer, level=level, message=msg))
            flush()

        def md(line: str) -> None:
            md_buf.append((line or "").rstrip("\n") + "\n")
            flush()

        def csv(row: str) -> None:
            csv_buf.append((row or "").rstrip("\n") + "\n")
            flush()

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
            flush(force=True)
        except Exception as e:
            log("fetch", "ERROR", f"Falha ao baixar HTML: {type(e).__name__}: {e}")
            audit.status = "error"
            flush(force=True)
            return

        md("# AUDIT_NEXUS — Professional Edition")
        md(f"- Target: {site.base_url}")
        md(f"- Final URL: {fetch.url}")
        md(f"- Provider: {base_url_v1}")
        md(f"- Model: {model}")
        md("")

        rows: list[str] = []
        consecutive_llm_failures = 0

        # Mode
        mode = "full"
        if (audit.logs or "").splitlines()[:1] and (audit.logs or "").splitlines()[0].startswith("MODE="):
            mode = (audit.logs or "").splitlines()[0].split("=", 1)[-1].strip().lower() or "full"
        layers = MICRO_LAYERS
        if mode == "fast":
            layers = [MICRO_LAYERS[0], MICRO_LAYERS[2], MICRO_LAYERS[6], MICRO_LAYERS[9]]  # 1,3,7,10
            log("system", "INFO", "Modo FAST ativo: executando camadas 1,3,7,10 para acelerar.")

        for i, layer in enumerate(layers, start=1):
            log(layer, "INFO", f"Iniciando {layer} ({i}/{len(layers)})...")
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
                flush(force=True)
                continue

            prompt = build_user_prompt(layer, fetch, cleaned)
            # Prefer streaming so the audit page updates in real time.
            try:
                log(layer, "INFO", "Chamando modelo (stream)…")
                for kind, text in stream_llm_events(
                    base_url_v1=base_url_v1,
                    api_key=api_key,
                    model=model,
                    temperature=0.2,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                ):
                    if kind == "HEARTBEAT":
                        log(layer, "INFO", text)
                    elif kind == "DATA":
                        md(text)
                    elif kind == "CSV_ROW":
                        row = (text or "").strip("\r").strip()
                        if not row or row.lower().startswith("categoria;"):
                            continue
                        if row.count(";") >= 6:
                            csv(row)
                            rows.append(row)
                consecutive_llm_failures = 0
                continue
            except Exception as e:
                log(layer, "WARN", f"Streaming falhou, fallback non-stream: {type(e).__name__}: {e}")

            try:
                content = call_llm_non_stream(
                    base_url_v1=base_url_v1,
                    api_key=api_key,
                    model=model,
                    temperature=0.2,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    timeout_s=120,
                )
                if not content.strip():
                    log(layer, "ERROR", "Resposta vazia do provedor LLM.")
                    consecutive_llm_failures += 1
                    if consecutive_llm_failures >= 2:
                        log("system", "ERROR", "Abortando auditoria: provedor LLM falhou repetidamente (>=2).")
                        audit.status = "error"
                        break
                    continue

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
                    if not row or row.lower().startswith("categoria;"):
                        continue
                    if row.count(";") >= 6:
                        csv(row)
                        rows.append(row)
                consecutive_llm_failures = 0
            except Exception as e:
                log(layer, "ERROR", f"Falha no provedor LLM: {type(e).__name__}: {e}")
                consecutive_llm_failures += 1
                if consecutive_llm_failures >= 2:
                    log("system", "ERROR", "Abortando auditoria: provedor LLM falhou repetidamente (>=2).")
                    audit.status = "error"
                    break
                continue

            flush(force=True)

        audit.status = "done" if audit.status != "error" else "error"
        flush(force=True)


def main() -> None:
    app = create_app()
    with app.app_context():
        redis_url = app.config["REDIS_URL"]
    conn = redis.from_url(redis_url)
    with Connection(conn):
        worker = Worker([Queue("audits"), Queue("ui")])
        worker.work()


if __name__ == "__main__":
    main()
