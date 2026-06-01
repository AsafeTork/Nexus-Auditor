from __future__ import annotations

import os


def mask_secret(s: str, keep: int = 4) -> str:
    """Mask all but the first `keep` characters of a secret string."""
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def get_master_email() -> str:
    return (os.getenv("MASTER_ADMIN_EMAIL", "") or "").strip().lower()
