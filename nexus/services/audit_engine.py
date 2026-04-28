from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Generator, List, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup


MAX_DOWNLOAD_BYTES = 5_000_000
MAX_CLEAN_HTML_CHARS = 100_000
CLEAN_HTML_TO_LLM_CHARS = 80_000

FETCH_TIMEOUT_S = 35
# LLM timeouts (tuneable via env to avoid long hangs on unstable providers)
LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "180"))
LLM_HEARTBEAT_S = 10

# Shared HTTP session for connection pooling (avoid creating a new TCP connection per call)
_HTTP = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
_HTTP.mount("http://", _ADAPTER)
_HTTP.mount("https://", _ADAPTER)


def normalize_base_url_v1(base_url_v1: str) -> str:
    """
    Normaliza o endpoint OpenAI-compatible.
    O usuário deve passar algo como:
      - https://host/v1
    (NÃO a rota completa /chat/completions)
    """
    u = (base_url_v1 or "").strip()
    if not u:
        return ""
    u = u.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    # Se o usuário colar a rota completa, removemos para evitar duplicação
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")].rstrip("/")
    return u


def list_models(*, base_url_v1: str, api_key: str, timeout_s: int = 12) -> List[str]:
    """
    Busca lista de modelos em provider OpenAI-compatible.
    GET {base}/models
    Retorna uma lista de IDs.
    """
    base = normalize_base_url_v1(base_url_v1)
    if not base:
        return []
    url = base.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.get(url, headers=headers, timeout=timeout_s)
    r.raise_for_status()
    j = r.json() or {}
    data = j.get("data") or []
    out: List[str] = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("id"):
                out.append(str(it["id"]))
    return out


def stream_llm_text(
    *,
    base_url_v1: str,
    api_key: str,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int = LLM_TIMEOUT_S,
) -> Generator[str, None, None]:
    """
    Streaming genérico (delta text) para providers OpenAI-compatible.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = normalize_base_url_v1(base_url_v1).rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": float(temperature),
        "stream": True,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    }
    r = _HTTP.post(url, headers=headers, json=payload, stream=True, timeout=timeout_s)
    r.encoding = "utf-8"
    r.raise_for_status()
    for raw in r.iter_lines(decode_unicode=True, chunk_size=2048):
        if not raw:
            continue
        line = str(raw).strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            obj = json.loads(line)
            delta = (((obj.get("choices") or [None])[0] or {}).get("delta") or {}).get("content") or ""
        except Exception:
            delta = ""
        if delta:
            yield str(delta)


MICRO_LAYERS = [
    "1. Headers & SSL (Infra)",
    "2. Vulnerabilidades de Script (Segurança)",
    "3. Core Web Vitals (Performance)",
    "4. Render Blocking & Assets (Performance)",
    "5. Meta-tags & Social (SEO)",
    "6. Hierarquia & JSON-LD (SEO)",
    "7. Fricção de UX (Conversão)",
    "8. Estratégia de Negócio (Econômico)",
    "9. UX Defensiva & Dark Patterns (SE)",
    "10. Executive Financial Summary (Financeiro)",
]


SYSTEM_PROMPT_DEFAULT = (
    "Você é um Perito Forense Web. Escreva com tom acadêmico, documental e profissional.\\n"
    "REGRA CRÍTICA: não invente vulnerabilidades. Só reporte uma falha se puder provar com um snippet literal do HTML/headers fornecidos.\\n"
    "Se NÃO houver prova, NÃO gere linha no CSV.\\n"
    "REGRAS DE OURO POR CAMADA:\\n"
    "- Para CADA camada, busque evidências específicas no HTML fornecido (até 80k chars) e nos headers.\\n"
    "- Para a camada 8 (Estratégia), se não houver evidência técnica, gere insight estratégico responsável baseado no texto/proposta do site (sem inventar fatos).\\n"
    "Retorne estritamente neste formato:\\n"
    "---REPORT---\\n"
    "## [Nome da Falha]\\n"
    "- **Prova:** [Snippet exato do HTML ou header literal]\\n"
    "- **Por quê:** [Motivo técnico]\\n"
    "- **Prejuízo:** [Impacto em USD]\\n"
    "- **Solução:** [Como corrigir]\\n"
    "---CSV---\\n"
    "Categoria;Falha;Prova Técnica;Explicação;Prejuízo Estimado;Solução;Prioridade;Complexity\\n"
    "[Uma linha por falha, delimitada por ';']\\n"
)


@dataclass
class FetchResult:
    url: str
    status_code: int
    elapsed_ms: int
    content_type: str
    headers: Dict[str, str]
    html: str


def fetch_url_html(url: str) -> FetchResult:
    t0 = time.time()
    headers = {
        "User-Agent": "NexusAuditor-Pro/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT_S, stream=True, allow_redirects=True)
    raw = bytearray()
    total = 0
    for chunk in r.iter_content(chunk_size=8192):
        if not chunk:
            continue
        raw.extend(chunk)
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            break
    elapsed_ms = int((time.time() - t0) * 1000)
    r.encoding = r.encoding or "utf-8"
    html = raw.decode(r.encoding, errors="replace")
    content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    return FetchResult(
        url=str(r.url),
        status_code=int(r.status_code),
        elapsed_ms=elapsed_ms,
        content_type=content_type,
        headers={k: str(v) for k, v in (r.headers or {}).items()},
        html=html,
    )


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["svg", "canvas", "iframe"]):
        try:
            tag.decompose()
        except Exception:
            pass
    out = str(soup)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > MAX_CLEAN_HTML_CHARS:
        out = out[:MAX_CLEAN_HTML_CHARS]
    return out


def build_user_prompt(layer: str, fetch: FetchResult, cleaned: str) -> str:
    headers_sample = {k.lower(): v for k, v in (fetch.headers or {}).items()}
    cleaned_preview = (cleaned or "")[:CLEAN_HTML_TO_LLM_CHARS]
    return (
        f"MICRO-CAMADA: {layer}\n"
        f"URL final: {fetch.url}\n"
        f"HTTP status: {fetch.status_code}\n"
        f"Tempo (ms): {fetch.elapsed_ms}\n\n"
        "HEADERS (literais / evidência):\n"
        f"content-security-policy: {headers_sample.get('content-security-policy')}\n"
        f"x-frame-options: {headers_sample.get('x-frame-options')}\n"
        f"strict-transport-security: {headers_sample.get('strict-transport-security')}\n"
        f"x-content-type-options: {headers_sample.get('x-content-type-options')}\n"
        f"referrer-policy: {headers_sample.get('referrer-policy')}\n\n"
        f"HTML LIMPO (literal, ATÉ {CLEAN_HTML_TO_LLM_CHARS} chars):\n{cleaned_preview}\n\n"
        "INSTRUÇÕES:\n"
        "- No CSV, sempre preencha a coluna 'Complexity' com: Baixa, Média ou Alta.\n"
    )


def stream_llm_events(
    *,
    base_url_v1: str,
    api_key: str,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
) -> Generator[Tuple[str, str], None, None]:
    """
    Minimal OpenAI-compatible streaming parser -> yields ("DATA"| "CSV_ROW" | "HEARTBEAT", text)
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = normalize_base_url_v1(base_url_v1).rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": float(temperature),
        "stream": True,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    }
    retry_statuses = {429, 502, 503, 504}
    backoffs = [2, 4, 8]
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            rr = _HTTP.post(url, headers=headers, json=payload, stream=True, timeout=LLM_TIMEOUT_S)
            rr.encoding = "utf-8"
            if rr.status_code in retry_statuses:
                # drain/close early to release the connection back to the pool
                try:
                    rr.close()
                except Exception:
                    pass
                last_exc = requests.HTTPError(f"{rr.status_code} Server Error for url: {url}")
                if attempt < 2:
                    time.sleep(backoffs[attempt])
                    continue
            rr.raise_for_status()
            r = rr
            last_exc = None
            break
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            if attempt < 2:
                time.sleep(backoffs[attempt])
                continue
        except Exception as e:
            # non-retryable
            last_exc = e
            break
    if r is None:
        raise last_exc or RuntimeError("Falha ao conectar ao provedor LLM.")

    buf = ""
    mode = "pre"
    last = time.time()

    for raw in r.iter_lines(decode_unicode=True, chunk_size=2048):
        if (time.time() - last) >= LLM_HEARTBEAT_S:
            yield ("HEARTBEAT", "[Heartbeat] Aguardando modelo...")
            last = time.time()
        if not raw:
            continue
        line = str(raw).strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            obj = json.loads(line)
            delta = (((obj.get("choices") or [None])[0] or {}).get("delta") or {}).get("content") or ""
        except Exception:
            delta = ""
        if not delta:
            continue
        last = time.time()
        buf += delta

        # very small state machine: ---REPORT--- then ---CSV---
        while True:
            if mode == "pre":
                i = buf.find("---REPORT---")
                if i < 0:
                    buf = buf[-16:]
                    break
                buf = buf[i + len("---REPORT---") :]
                mode = "report"
                continue
            if mode == "report":
                i = buf.find("---CSV---")
                if i < 0:
                    if "\n" in buf:
                        parts = buf.split("\n")
                        for ln in parts[:-1]:
                            yield ("DATA", ln)
                        buf = parts[-1]
                    break
                report_part = buf[:i]
                for ln in report_part.split("\n"):
                    if ln.strip():
                        yield ("DATA", ln)
                buf = buf[i + len("---CSV---") :]
                mode = "csv"
                continue
            if mode == "csv":
                if "\n" in buf:
                    parts = buf.split("\n")
                    for ln in parts[:-1]:
                        if ln.strip():
                            yield ("CSV_ROW", ln.strip("\r"))
                    buf = parts[-1]
                break


def call_llm_non_stream(
    *,
    base_url_v1: str,
    api_key: str,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int = 180,
) -> str:
    """
    Safer fallback for providers that stall/hang on streaming.
    Returns the assistant message content (string).
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = normalize_base_url_v1(base_url_v1).rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": float(temperature),
        "stream": False,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    }

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
            r.encoding = "utf-8"
            # Cloudflare / upstream timeouts
            if r.status_code in (520, 524, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                last_exc = requests.HTTPError(f"{r.status_code} Server Error for url: {url}")
                continue
            r.raise_for_status()
            data = r.json()
            try:
                return str(((data.get("choices") or [None])[0] or {}).get("message", {}).get("content") or "")
            except Exception:
                return ""
        except Exception as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
            continue

    if last_exc:
        raise last_exc
    try:
        return ""
    except Exception:
        return ""

def estimate_ltv_loss_from_rows(rows: List[str]) -> Tuple[int, int]:
    """
    Heuristic range (USD) from priorities in CSV rows.
    """
    text = "\n".join(rows).lower()
    p_hi = text.count(";alta;")
    p_med = text.count(";média;") + text.count(";media;")
    p_low = max(0, len(rows) - p_hi - p_med)
    base = p_hi * 15000 + p_med * 5000 + p_low * 1500
    return int(base * 0.7), int(base * 1.4)
