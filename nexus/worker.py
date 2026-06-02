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
    fetch_url_html,
    stream_llm_events,
    stream_llm_text,
)
from .services.research import get_or_refresh_attack_benchmarks
try:
    # Optional: continuous monitoring modules may be missing in some deployments.
    from .services.monitoring import persist_monitoring_history  # type: ignore
except Exception:
    persist_monitoring_history = None  # type: ignore
from .services.ui_review import read_text_files, summarize_screenshot


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
                append_log(f"[ui-lab] reading templates ({len(targets)} files)…")

                # Keep per-file size bounded and also bound the whole context to avoid LLM failures.
                max_each = int(os.getenv("UI_LAB_MAX_CHARS_EACH", "12000"))
                max_total = int(os.getenv("UI_LAB_MAX_CONTEXT_CHARS", "180000"))
                blob = read_text_files([p for p in targets if os.path.exists(p)], max_chars_each=max_each)
                if len(blob) > max_total:
                    blob = blob[:max_total] + "\n\n/* ... context truncated ... */\n"
                    append_log(f"[ui-lab] context truncated to {max_total} chars")
                context = blob

                # Optional: also scan all org sites (domains) and attach HTML + screenshots metadata.
                sites = Site.query.filter_by(org_id=org_id).all()
                append_log(f"[ui-lab] collecting org sites ({len(sites)})…")
                if sites:
                    import requests

                    site_ctx_parts: list[str] = []
                    max_site_html = int(os.getenv("UI_LAB_MAX_SITE_HTML_CHARS", "12000"))

                    def _try_screenshot(url: str) -> dict:
                        """
                        Attempt a real browser screenshot using Playwright.
                        The screenshot bytes are NOT persisted and are discarded after summarization.
                        """
                        try:
                            from playwright.sync_api import sync_playwright  # type: ignore
                        except Exception as e:
                            return {"ok": False, "error": f"playwright_not_available: {type(e).__name__}"}
                        try:
                            with sync_playwright() as p:
                                browser = p.chromium.launch(args=["--no-sandbox"])
                                page = browser.new_page(viewport={"width": 1440, "height": 900})
                                # Validate URL with SSRF guard before navigating the browser.
                                safe = fetch_url_html(url).url
                                page.goto(safe, wait_until="networkidle", timeout=60000)
                                img_bytes = page.screenshot(full_page=True, type="png")
                                browser.close()
                            meta = summarize_screenshot(img_bytes).__dict__
                            # Discard bytes immediately (do not store anywhere).
                            del img_bytes
                            return {"ok": True, "meta": meta}
                        except Exception as e:
                            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

                    for s in sites:
                        url = (s.base_url or "").strip()
                        if not url:
                            continue
                        append_log(f"[ui-lab] site: {url}")
                        # HTML
                        html_snip = ""
                        try:
                            # Reuse audit SSRF guard: fetch_url_html validates target is public http(s)
                            ff = fetch_url_html(url)
                            html_snip = (ff.html or "")[:max_site_html]
                        except Exception as e:
                            html_snip = f"<!-- HTML fetch failed: {type(e).__name__}: {e} -->"
                        # Screenshot meta
                        shot = _try_screenshot(url)
                        site_ctx_parts.append(
                            "\n\n===== ORG SITE =====\n"
                            + f"site_id={s.id}\n"
                            + f"url={url}\n"
                            + f"screenshot={shot}\n"
                            + f"html_snippet:\n{html_snip}\n"
                        )

                    site_blob = "\n".join(site_ctx_parts)
                    if site_blob:
                        context = context + "\n\n===== ORG SITES (AUTO) =====\n" + site_blob
            elif mode == "url":
                import requests

                url = str(payload.get("url") or "").strip()
                append_log(f"[ui-lab] fetching HTML: {url}")
                # Reuse audit SSRF guard: validates target is public http(s)
                ff = fetch_url_html(url)
                html = ff.html or ""
                if len(html) > 20000:
                    html = html[:20000] + "\n<!-- ... truncated ... -->"
                context = f"URL: {url}\n\nHTML:\n{html}"
            elif mode == "screenshot":
                meta = payload.get("meta") or {}
                notes = str(payload.get("notes") or "")
                context = f"Screenshot meta: {meta}\nNotes: {notes}\n"
            elif mode == "backend":
                # Backend prompt generator: read backend source files (Python).
                root = os.path.abspath(os.path.dirname(__file__))
                targets: list[str] = []
                for r, dirs, files in os.walk(root):
                    # Skip caches
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    for fn in files:
                        if not fn.endswith(".py"):
                            continue
                        targets.append(os.path.join(r, fn))
                targets = sorted(set(targets))
                append_log(f"[backend-lab] reading backend ({len(targets)} files)…")
                max_each = int(os.getenv("BACKEND_LAB_MAX_CHARS_EACH", "12000"))
                max_total = int(os.getenv("BACKEND_LAB_MAX_CONTEXT_CHARS", "220000"))
                blob = read_text_files([p for p in targets if os.path.exists(p)], max_chars_each=max_each)
                if len(blob) > max_total:
                    blob = blob[:max_total] + "\n\n/* ... context truncated ... */\n"
                    append_log(f"[backend-lab] context truncated to {max_total} chars")
                context = blob
            else:
                context = f"Unknown mode: {mode}\nPayload: {payload}"
        except Exception as e:
            append_log(f"[ui-lab] error building context: {type(e).__name__}: {e}")
            try:
                conn.hset(key, mapping={"status": "error", "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
            return

        goal = str(payload.get("goal") or "").strip() or "Make the UI more premium, clear, and consistent."

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
            api_key = str(
                payload.get("api_key")
                or (getattr(org, "llm_api_key", "") if org else "")
                or app.config.get("LLM_API_KEY", "")
            ).strip()
            append_log(f"[ui-lab] calling LLM (stream)… model={model}")

            acc = ""
            last_flush = time.time()
            # v2: output a single PROMPT to feed back into SOLO (not a long report).
            if mode == "backend":
                system_prompt = (
                    "You are a Staff Backend Engineer. "
                    "Your output is NOT a report for humans to read. "
                    "Your output must be a SINGLE PROMPT ready to paste into SOLO for it to implement backend changes. "
                    "Prioritize: security, reliability, performance, observability (logs/metrics), "
                    "error handling, data consistency, jobs/queues, timeouts, and tests. "
                    "Do not invent files that do not exist. "
                    "Required format:\n"
                    "1) TITLE: \"PROMPT FOR SOLO (BACKEND)\"\n"
                    "2) CONTEXT (2-3 lines)\n"
                    "3) GOAL (bullets)\n"
                    "4) RULES (bullets)\n"
                    "5) CHANGE PLAN BY FILE (checklist with real paths)\n"
                    "6) CODE SNIPPETS (only when necessary, kept short)\n"
                    "7) HOW TO VALIDATE (commands and scenarios)\n"
                    "Be direct and actionable."
                )
            else:
                system_prompt = (
                    "You are a senior Product Designer + Frontend Engineer. "
                    "Your output is NOT a report for humans to read. "
                    "Your output must be a SINGLE PROMPT ready to paste into SOLO for it to implement code changes. "
                    "Requirements: (1) focus/hierarchy/vertical rhythm, (2) consistent spacing, (3) responsive mobile+desktop, "
                    "(4) accessibility (aria/keyboard/focus/contrast), (5) do not invent files that do not exist. "
                    "Required format:\n"
                    "1) TITLE: \"PROMPT FOR SOLO\"\n"
                    "2) CONTEXT (2-3 lines)\n"
                    "3) GOAL (bullets)\n"
                    "4) RULES (bullets)\n"
                    "5) CHANGE PLAN BY FILE (checklist with real paths)\n"
                    "6) CODE SNIPPETS (only when necessary, kept short)\n"
                    "7) HOW TO VALIDATE (quick steps)\n"
                    "Be direct and actionable."
                )

            for delta in stream_llm_text(
                base_url_v1=base_url_v1,
                api_key=api_key,
                model=model,
                temperature=0.2,
                system_prompt=system_prompt,
                user_prompt=f"Goal: {goal}\n\nContext:\n{context}\n",
                timeout_s=240,
            ):
                acc += delta
                if time.time() - last_flush > 1.0:
                    conn.set(key + ":result", acc, ex=60 * 60 * 24 * 7)
                    last_flush = time.time()

            if not acc.strip():
                raise RuntimeError("Empty response from LLM.")
            conn.set(key + ":result", acc, ex=60 * 60 * 24 * 7)
            conn.hset(key, mapping={"status": "done"})
            append_log("[ui-lab] done")
        except Exception as e:
            append_log(f"[ui-lab] LLM error: {type(e).__name__}: {e}")
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
            """
            Append CSV row if it is well-formed and not a duplicate (category+failure key).
            """
            rr = (row or "").strip().strip("\r").strip("\n")
            if not rr:
                return
            if rr.lower().startswith("categoria;"):
                return
            parts = [p.strip() for p in rr.split(";")]
            if len(parts) < 7:
                return
            key = (parts[0].lower() + "|" + parts[1].lower()).strip()
            if not key or key in seen_rows:
                return
            seen_rows.add(key)
            csv_buf.append(rr + "\n")
            flush()

        site = Site.query.filter_by(id=audit.site_id).first()
        if not site:
            audit.status = "error"
            db.session.commit()
            return

        base_url_v1 = audit.provider_base_url_v1 or app.config["LLM_BASE_URL_V1"]
        org = Organization.query.filter_by(id=audit.org_id).first()
        api_key = str((getattr(org, "llm_api_key", "") if org else "") or app.config.get("LLM_API_KEY", "")).strip()
        model = audit.model or app.config["LLM_DEFAULT_MODEL"]
        system_prompt = SYSTEM_PROMPT_DEFAULT
        audit_brief = (os.getenv("AUDIT_BRIEF", "") or "").strip()
        reflect = str(os.getenv("AUDIT_REFLECT", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
        attack_total_mode = str(os.getenv("AUDIT_ATTACK_TOTAL_MODE", "security") or "security").strip().lower()
        max_consecutive_llm_failures = int(os.getenv("AUDIT_MAX_CONSECUTIVE_LLM_FAILURES", "4") or "4")
        max_consecutive_llm_failures = max(2, min(10, max_consecutive_llm_failures))

        try:
            log("fetch", "INFO", f"Fetching HTML: {site.base_url}")
            fetch = fetch_url_html(site.base_url)
            cleaned = clean_html(fetch.html)
            host = (urlparse(fetch.url).hostname or "")
            audit.target_domain = host
            flush(force=True)
        except Exception as e:
            log("fetch", "ERROR", f"Failed to fetch HTML: {type(e).__name__}: {e}")
            audit.status = "error"
            flush(force=True)
            return

        md("# Audit Report")
        md(f"- Target: {site.base_url}")
        md(f"- Final URL: {fetch.url}")
        md(f"- Provider: {base_url_v1}")
        md(f"- Model: {model}")
        md("")

        rows: list[str] = []
        seen_rows: set[str] = set()
        consecutive_llm_failures = 0
        llm_failed_any = False

        # Mode
        mode = "full"
        if (audit.logs or "").splitlines()[:1] and (audit.logs or "").splitlines()[0].startswith("MODE="):
            mode = (audit.logs or "").splitlines()[0].split("=", 1)[-1].strip().lower() or "full"
        layers = MICRO_LAYERS
        if mode == "fast":
            layers = [MICRO_LAYERS[0], MICRO_LAYERS[2], MICRO_LAYERS[6], MICRO_LAYERS[9]]  # 1,3,7,10
            log("system", "INFO", "FAST mode: running layers 1, 3, 7, 10.")

        for i, layer in enumerate(layers, start=1):
            log(layer, "INFO", f"Starting {layer} ({i}/{len(layers)})...")
            md(f"## {layer}")



            prompt = build_user_prompt(layer, fetch, cleaned, brief=audit_brief)

            # Reflection mode: non-stream draft + refinement pass (do not expose chain-of-thought; only final output)
            if reflect:
                try:
                    log(layer, "INFO", "Calling model (non-stream + reflection)…")
                    draft = call_llm_non_stream(
                        base_url_v1=base_url_v1,
                        api_key=api_key,
                        model=model,
                        temperature=0.15,
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        timeout_s=140,
                    )
                    reviewer_prompt = (
                        "You are a senior reviewer. Goal: improve quality WITHOUT inventing facts.\n"
                        "Rules:\n"
                        "- Remove duplicates and generic items.\n"
                        "- If an item has no literal proof, DELETE it from the CSV.\n"
                        "- Use conservative ranges or 'N/A' for financial impact if there is no basis.\n"
                        "- Do NOT write step-by-step chain-of-thought. Generate only a short summary of why.\n"
                        "- Include a collapsible block in the report:\n"
                        "  <details><summary>Reasoning (summary)</summary>\n"
                        "  - 2 to 5 bullets quickly explaining why the findings matter, citing evidence.\n"
                        "  </details>\n"
                        "- Keep the same strict format: ---REPORT--- ... ---CSV--- ...\n"
                        "\nORIGINAL PROMPT (context):\n"
                        + prompt
                        + "\n\nDRAFT TO REVIEW:\n"
                        + (draft or "")
                    )
                    final = call_llm_non_stream(
                        base_url_v1=base_url_v1,
                        api_key=api_key,
                        model=model,
                        temperature=0.05,
                        system_prompt=system_prompt,
                        user_prompt=reviewer_prompt,
                        timeout_s=160,
                    )
                    content = final or draft or ""
                except Exception as e:
                    log(layer, "WARN", f"Reflection failed, falling back to non-stream: {type(e).__name__}: {e}")
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
                    except Exception as e2:
                        # Treat as an essential LLM failure (avoid "running forever" / false success).
                        log(layer, "ERROR", f"LLM provider error (fallback): {type(e2).__name__}: {e2}")
                        llm_failed_any = True
                        consecutive_llm_failures += 1
                        audit.status = "error"
                        break

                if not content.strip():
                    log(layer, "ERROR", "Empty response from LLM provider.")
                    llm_failed_any = True
                    consecutive_llm_failures += 1
                    if consecutive_llm_failures >= max_consecutive_llm_failures:
                        log("system", "ERROR", f"Aborting audit: LLM provider failed repeatedly (>={max_consecutive_llm_failures}).")
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
                        parts = [p.strip() for p in row.split(";")]
                        if len(parts) >= 2:
                            key = (parts[0].lower() + "|" + parts[1].lower()).strip()
                            if key and key in seen_rows:
                                rows.append(row)
                consecutive_llm_failures = 0
                flush(force=True)
                continue

            # Default: prefer streaming so the audit page updates in real time.
            try:
                log(layer, "INFO", "Calling model (stream)…")
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
                            # Store de-duped canonical rows for summary calc
                            parts = [p.strip() for p in row.split(";")]
                            if len(parts) >= 2:
                                key = (parts[0].lower() + "|" + parts[1].lower()).strip()
                                if key and key in seen_rows:
                                    rows.append(row)
                consecutive_llm_failures = 0
                continue
            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                # 429 = provider rate limit; no point trying more layers, abort now
                if "429" in err_str:
                    log(layer, "ERROR", f"Provider rate limit (429). Switch to a different provider in Settings and run a new audit. {err_str}")
                    audit.status = "error"
                    flush(force=True)
                    return
                log(layer, "WARN", f"Streaming failed, falling back to non-stream: {err_str}")

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
                    log(layer, "ERROR", "Empty response from LLM provider.")
                    consecutive_llm_failures += 1
                    if consecutive_llm_failures >= max_consecutive_llm_failures:
                        log("system", "ERROR", f"Aborting audit: LLM provider failed repeatedly (>={max_consecutive_llm_failures}).")
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
                        parts = [p.strip() for p in row.split(";")]
                        if len(parts) >= 2:
                            key = (parts[0].lower() + "|" + parts[1].lower()).strip()
                            if key and key in seen_rows:
                                rows.append(row)
                consecutive_llm_failures = 0
            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                if "429" in err_str:
                    log(layer, "ERROR", f"Provider rate limit (429). Switch to a different provider in Settings and run a new audit. {err_str}")
                    audit.status = "error"
                    flush(force=True)
                    return
                log(layer, "ERROR", f"LLM provider error: {err_str}")
                llm_failed_any = True
                consecutive_llm_failures += 1
                if consecutive_llm_failures >= max_consecutive_llm_failures:
                    log("system", "ERROR", f"Aborting audit: LLM provider failed repeatedly (>={max_consecutive_llm_failures}).")
                    audit.status = "error"
                    break
                continue

            flush(force=True)

        # Product correctness: if the LLM failed, the report is incomplete.
        # Do NOT mark as done (avoids false confidence).
        if audit.status != "error" and llm_failed_any:
            audit.status = "error"
            log("system", "ERROR", "Audit incomplete: LLM provider failed. Status set to error.")

        audit.status = "done" if audit.status != "error" else "error"

        # Continuous monitoring history + diff (best-effort, non-blocking for audits).
        try:
            if persist_monitoring_history:
                persist_monitoring_history(audit)
        except Exception:
            pass



        # Optional: Market research benchmarks (CatchAll / Newscatcher)
        try:
            market_enabled = str(os.getenv("AUDIT_MARKET_RESEARCH", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
            if market_enabled:
                bench = get_or_refresh_attack_benchmarks(conn)
                if bench and (bench.get("citations") or []):
                    md("## Market benchmarks (web research)")
                    md("- Sources found via CatchAll (Newscatcher).")
                    for c in bench.get("citations") or []:
                        title = (c.get("title") or "").strip() or "Source"
                        link = (c.get("link") or "").strip()
                        dt = (c.get("published_date") or "").strip()
                        if dt:
                            md(f"- [{title}]({link}) — {dt}")
                        else:
                            md(f"- [{title}]({link})")
                    md("")
                    md("<details><summary>Reasoning (summary)</summary>")
                    md("- The estimated total above is the sum of USD ranges for findings with technical evidence in this audit.")
                    md("- The sources above serve as market benchmarks (real reported/published costs).")
                    md("- If the benchmark indicates orders of magnitude higher/lower, adjust the ranges by scope (data, revenue, downtime, compliance).")
                    md("</details>")
                    md("")
                    flush(force=True)
                else:
                    md("## Market benchmarks (web research)")
                    md("- Research started via CatchAll (Newscatcher). **May take ~10–15 minutes** to complete.")
                    md("- Run another audit later or click the refresh icon to pull from cache once ready.")
                    md("")
                    flush(force=True)
        except Exception:
            pass

        flush(force=True)


def main() -> None:
    app = create_app()
    with app.app_context():
        redis_url = app.config["REDIS_URL"]
    conn = redis.from_url(redis_url)
    with Connection(conn):
        # Pass explicit connection to avoid falling back to localhost in some environments.
        q_audits = Queue("audits", connection=conn)
        q_ui = Queue("ui", connection=conn)
        worker = Worker([q_audits, q_ui], connection=conn)
        worker.work()


if __name__ == "__main__":
    main()
