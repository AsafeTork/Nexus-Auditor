from __future__ import annotations

from typing import Any, Dict

from flask import current_app


_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "skip_to_content": "Skip to content",
        "back": "Back",
        "copy": "Copy",
        "copied": "Copied.",
        "logout": "Logout",
    },
    "pt-br": {
        "skip_to_content": "Pular para o conteúdo",
        "back": "Voltar",
        "copy": "Copiar",
        "copied": "Copiado.",
        "logout": "Sair",
    },
}


def get_lang() -> str:
    """
    Returns the current UI language (never crashes; always falls back to 'en').
    """
    try:
        lang = str(current_app.config.get("APP_LANG", "en") or "en").strip().lower()
    except Exception:
        lang = "en"
    return lang or "en"


def t(key: str, default: str | None = None, lang: str | None = None) -> str:
    """
    Template-safe translator.
    - Never raises.
    - Falls back to EN, then to default, then to key.
    """
    try:
        lang2 = (lang or get_lang()).strip().lower()
        table = _STRINGS.get(lang2) or _STRINGS.get("en") or {}
        if key in table:
            return table[key]
        if key in (_STRINGS.get("en") or {}):
            return (_STRINGS.get("en") or {}).get(key) or (default or key)
        return default or key
    except Exception:
        return default or key

