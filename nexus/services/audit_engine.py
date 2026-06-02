from __future__ import annotations

import json
import os
import re
import socket
import time
import hashlib
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Dict, Generator, List, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
import redis

from .llm_providers import call_provider_non_stream, list_provider_models, normalize_provider


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

# Redis cache (best-effort; disabled if Redis is unavailable)
_REDIS: redis.Redis | None = None


def _redis_conn() -> redis.Redis | None:
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    try:
        url = os.getenv("REDIS_URL", "")
        if not url:
            return None
        _REDIS = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
        return _REDIS
    except Exception:
        return None


def _llm_cache_key(
    *,
    kind: str,
    base_url_v1: str,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """
    Stable cache key for identical inputs (minimize LLM costs).
    """
    h = hashlib.sha256()
    h.update((kind or "llm").encode("utf-8"))
    h.update(b"\n")
    h.update((normalize_base_url_v1(base_url_v1) or "").encode("utf-8"))
    h.update(b"\n")
    h.update((model or "").encode("utf-8"))
    h.update(b"\n")
    h.update((str(float(temperature))).encode("utf-8"))
    h.update(b"\n")
    h.update((system_prompt or "").encode("utf-8"))
    h.update(b"\n")
    h.update((user_prompt or "").encode("utf-8"))
    return "llm:" + h.hexdigest()


def _cache_get_text(key: str) -> str | None:
    r = _redis_conn()
    if not r:
        return None
    try:
        return r.get(key)
    except Exception:
        return None


def _cache_set_text(key: str, value: str, ttl_s: int) -> None:
    r = _redis_conn()
    if not r:
        return
    try:
        # Keep cached payloads bounded to avoid blowing up Redis memory.
        if value and len(value) > 400_000:
            value = value[:400_000]
        r.set(key, value, ex=int(ttl_s))
    except Exception:
        return


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





def list_models(*, base_url_v1: str, api_key: str, timeout_s: int = 12, provider: str = "") -> List[str]:
    """
    Busca lista de modelos em provider OpenAI-compatible.
    GET {base}/models
    Retorna uma lista de IDs.
    """
    base = normalize_base_url_v1(base_url_v1)
    if not base:
        return []
    return list_provider_models(
        provider=normalize_provider(provider, base),
        base_url_v1=base,
        api_key=api_key,
        timeout_s=timeout_s,
    )


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
    provider = normalize_provider("", base_url_v1)
    if provider == "anthropic":
        # Minimal compatibility: Anthropic direct is handled as non-stream and yielded once.
        text = call_provider_non_stream(
            provider=provider,
            base_url_v1=base_url_v1,
            api_key=api_key,
            model=model,
            temperature=temperature,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_s=timeout_s,
        )
        if text:
            yield text
        return

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
    cache_key = _llm_cache_key(
        kind="stream_text",
        base_url_v1=base_url_v1,
        model=model,
        temperature=temperature,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    cached = _cache_get_text(cache_key)
    if cached:
        yield cached
        return

    r = _HTTP.post(url, headers=headers, json=payload, stream=True, timeout=timeout_s)
    r.encoding = "utf-8"
    r.raise_for_status()
    acc = ""
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
            s = str(delta)
            acc += s
            yield s
    if acc.strip():
        _cache_set_text(cache_key, acc, ttl_s=int(os.getenv("LLM_CACHE_TTL_S", str(60 * 60 * 24 * 7))))


MICRO_LAYERS = [
    "1. Fluxo de Checkout & Pagamento (Risco Imediato)",
    "2. Sessão & Autenticação de Compradores (Abandono)",
    "3. Sinais de Confiança TLS/SSL (Atrito antes da Compra)",
    "4. Velocidade e Core Web Vitals (Queda de Conversão)",
    "5. Scripts e Componentes Expostos (Risco Operacional)",
    "6. UX Defensiva e Redirecionamentos (Erosão de Confiança)",
]


SYSTEM_PROMPT_DEFAULT = (
    "Você é um Agente de Proteção de Receita (Revenue Protection) para E-commerce e SaaS.\\n"
    "Seu objetivo exclusivo é identificar falhas técnicas no front-end, DOM e headers que causam perda de vendas, atrito e abandono de carrinho.\\n"
    "\\n"
    "REGRA CRÍTICA (anti-hallucination):\\n"
    "- Só reporte um problema se conseguir PROVAR com um snippet literal do HTML/headers fornecidos.\\n"
    "- Se NÃO houver prova literal, NÃO gere linha no CSV.\\n"
    "\\n"
    "FOCO EM NEGÓCIOS:\\n"
    "- Todo título de falha DEVE usar estritamente a fórmula:\\n"
    "  Antes era: 'Missing security headers'\\n"
    "  Agora o Título DEVE SER: '⚠️ [Como afeta o cliente] → [Como isso derruba vendas]'\\n"
    "  Exemplo perfeito: '⚠️ Seu checkout pode parecer inseguro para clientes → isso reduz conversão e impacta vendas'\\n"
    "Retorne estritamente neste formato:\\n"
    "---REPORT---\\n"
    "## ⚠️ [Como afeta o cliente] → [Como isso derruba vendas]\\n"
    "- **Prova:** [Snippet exato do HTML ou header]\\n"
    "- **Por que custa dinheiro:** [Como afeta o fluxo de compra/usuário]\\n"
    "- **Mecanismo de Perda:** [Como a conversão cai]\\n"
    "- **Solução:** [Medida corretiva técnica]\\n"
    "---CSV---\\n"
    "Categoria;Technical_Type;Título Comercial;Prova Técnica;Explicação;Mecanismo;Solução;Prioridade;Complexidade\\n"
    "[Uma linha por risco identificado, delimitada por ';']\\n"
    "Exemplo Technical_Type: payment_timeout, hsts_missing, ssl_expired, cls_high, brittle_selector, jwt_vulnerability.\\n"
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
    # SSRF protection: allow only public http(s) targets
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise ValueError("URL inválida: use http/https.")
    host = (p.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL inválida: host ausente.")
    if host in ("localhost",) or host.endswith(".local"):
        raise ValueError("Host bloqueado (SSRF).")

    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        ips = {info[4][0] for info in infos if info and info[4]}
    except Exception as e:
        raise ValueError(f"Falha ao resolver DNS do host: {host}") from e

    for ip in ips:
        try:
            ip_obj = ip_address(ip)
        except Exception:
            continue
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise ValueError(f"Host/IP bloqueado (SSRF): {host} -> {ip}")

    t0 = time.time()
    headers = {
        "User-Agent": "AuditAgent/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # Follow redirects manually and re-validate each hop
    current = url
    r = None
    for _ in range(5):
        rr = requests.get(current, headers=headers, timeout=FETCH_TIMEOUT_S, stream=True, allow_redirects=False)
        if rr.is_redirect or rr.is_permanent_redirect:
            loc = rr.headers.get("Location", "")
            rr.close()
            if not loc:
                r = rr
                break
            nxt = requests.compat.urljoin(current, loc)
            pp = urlparse(nxt)
            hh = (pp.hostname or "").strip().lower()
            if not hh:
                raise ValueError("Redirect inválido.")
            if hh in ("localhost",) or hh.endswith(".local"):
                raise ValueError("Redirect bloqueado (SSRF).")
            try:
                infos2 = socket.getaddrinfo(hh, pp.port or (443 if pp.scheme == "https" else 80), type=socket.SOCK_STREAM)
                ips2 = {info[4][0] for info in infos2 if info and info[4]}
            except Exception as e:
                raise ValueError(f"Falha ao resolver DNS do redirect: {hh}") from e
            for ip2 in ips2:
                try:
                    ip_obj2 = ip_address(ip2)
                except Exception:
                    continue
                if (
                    ip_obj2.is_private
                    or ip_obj2.is_loopback
                    or ip_obj2.is_link_local
                    or ip_obj2.is_multicast
                    or ip_obj2.is_reserved
                    or ip_obj2.is_unspecified
                ):
                    raise ValueError(f"Redirect bloqueado (SSRF): {hh} -> {ip2}")
            current = nxt
            continue
        r = rr
        break
    if r is None:
        raise ValueError("Falha ao seguir redirect.")
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


def build_user_prompt(layer: str, fetch: FetchResult, cleaned: str, brief: str = "") -> str:
    headers_sample = {k.lower(): v for k, v in (fetch.headers or {}).items()}
    cleaned_preview = (cleaned or "")[:CLEAN_HTML_TO_LLM_CHARS]
    brief2 = (brief or "").strip()
    if len(brief2) > 8000:
        brief2 = brief2[:8000]
    parts: list[str] = [
        f"MICRO-CAMADA: {layer}\n"
        f"URL final: {fetch.url}\n"
        f"HTTP status: {fetch.status_code}\n"
        f"Tempo (ms): {fetch.elapsed_ms}\n\n"
    ]
    if brief2:
        parts.append(f"BRIEF DO PRODUTO (contexto, não inventar fatos):\n{brief2}\n\n")
    parts.append(
        "HEADERS (literais / evidência):\n"
        f"content-security-policy: {headers_sample.get('content-security-policy')}\n"
        f"x-frame-options: {headers_sample.get('x-frame-options')}\n"
        f"strict-transport-security: {headers_sample.get('strict-transport-security')}\n"
        f"x-content-type-options: {headers_sample.get('x-content-type-options')}\n"
        f"referrer-policy: {headers_sample.get('referrer-policy')}\n\n"
        f"HTML LIMPO (literal, ATÉ {CLEAN_HTML_TO_LLM_CHARS} chars):\n{cleaned_preview}\n\n"
        "INSTRUÇÕES:\n"
        "- Extraia SOMENTE riscos que geram atrito no funil de vendas, checkout, confiança do consumidor ou risco grave operacional.\n"
        "- Prioridades permitidas: Alta, Média, Baixa.\n"
        "- No CSV, separe os dados com ponto e vírgula (;).\n"
    )
    return "".join(parts)


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
    cache_key = _llm_cache_key(
        kind="stream_events",
        base_url_v1=base_url_v1,
        model=model,
        temperature=temperature,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    cached = _cache_get_text(cache_key)
    if cached:
        # cached format: ---REPORT---\n...\n---CSV---\n...
        try:
            content2 = cached.split("---REPORT---", 1)[1] if "---REPORT---" in cached else cached
        except Exception:
            content2 = cached
        report, csv_block = (content2.split("---CSV---", 1) + [""])[:2] if "---CSV---" in content2 else (content2, "")
        for ln in report.splitlines():
            if ln.strip():
                yield ("DATA", ln)
        for ln in csv_block.splitlines():
            if ln.strip():
                yield ("CSV_ROW", ln.strip("\r"))
        return

    retry_statuses = {502, 503, 504}  # 429 = rate-limited; retrying makes it worse, fail immediately
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
        raise last_exc or RuntimeError("Failed to connect to LLM provider.")

    buf = ""
    mode = "pre"
    last = time.time()
    report_lines: list[str] = []
    csv_lines: list[str] = []

    for raw in r.iter_lines(decode_unicode=True, chunk_size=2048):
        if (time.time() - last) >= LLM_HEARTBEAT_S:
            yield ("HEARTBEAT", "[Heartbeat] Waiting for model...")
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
                            report_lines.append(ln)
                            yield ("DATA", ln)
                        buf = parts[-1]
                    break
                report_part = buf[:i]
                for ln in report_part.split("\n"):
                    if ln.strip():
                        report_lines.append(ln)
                        yield ("DATA", ln)
                buf = buf[i + len("---CSV---") :]
                mode = "csv"
                continue
            if mode == "csv":
                if "\n" in buf:
                    parts = buf.split("\n")
                    for ln in parts[:-1]:
                        if ln.strip():
                            csv_lines.append(ln.strip("\r"))
                            yield ("CSV_ROW", ln.strip("\r"))
                    buf = parts[-1]
                break

    # store cache as canonical content to allow replay
    if report_lines or csv_lines:
        cache_payload = "---REPORT---\n" + "\n".join(report_lines).strip() + "\n---CSV---\n" + "\n".join(csv_lines).strip()
        _cache_set_text(cache_key, cache_payload, ttl_s=int(os.getenv("LLM_CACHE_TTL_S", str(60 * 60 * 24 * 7))))


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
    provider = normalize_provider("", base_url_v1)

    cache_key = _llm_cache_key(
        kind="non_stream",
        base_url_v1=base_url_v1,
        model=model,
        temperature=temperature,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    cached = _cache_get_text(cache_key)
    if cached:
        return cached

    last_exc = None
    max_attempts = int(os.getenv("LLM_NONSTREAM_RETRIES", "4") or "4")
    max_attempts = max(2, min(8, max_attempts))
    for attempt in range(max_attempts):
        try:
            out = call_provider_non_stream(
                provider=provider,
                base_url_v1=base_url_v1,
                api_key=api_key,
                model=model,
                temperature=temperature,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=timeout_s,
            )
            if out.strip():
                _cache_set_text(cache_key, out, ttl_s=int(os.getenv("LLM_CACHE_TTL_S", str(60 * 60 * 24 * 7))))
            return out
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

