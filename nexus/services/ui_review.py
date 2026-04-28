from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from typing import Iterable


@dataclass
class ScreenshotMeta:
    width: int
    height: int
    fmt: str
    size_bytes: int
    dominant_hex: list[str]


def summarize_screenshot(file_bytes: bytes) -> ScreenshotMeta:
    """
    Lightweight screenshot summarizer (no vision model required).
    Extracts: size, format, and a few dominant colors (approx).
    """
    try:
        from PIL import Image
    except Exception:
        return ScreenshotMeta(0, 0, "unknown", len(file_bytes), [])

    img = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
    w, h = img.size
    small = img.resize((128, int(128 * h / max(w, 1))), Image.LANCZOS)
    # quantize to palette
    q = small.convert("P", palette=Image.ADAPTIVE, colors=8)
    pal = q.getpalette()
    # count
    from collections import Counter

    cnt = Counter(q.getdata())
    dominant = []
    for idx, _c in cnt.most_common(6):
        r, g, b = pal[idx * 3 : idx * 3 + 3]
        dominant.append(f"#{r:02x}{g:02x}{b:02x}")
    return ScreenshotMeta(w, h, (img.format or "PNG"), len(file_bytes), dominant)


def read_text_files(paths: Iterable[str], max_chars_each: int = 12000) -> str:
    parts: list[str] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except Exception:
            continue
        if len(txt) > max_chars_each:
            txt = txt[:max_chars_each] + "\n\n/* ... truncado ... */\n"
        parts.append(f"\n\n===== FILE: {p} =====\n{txt}")
    return "\n".join(parts).strip()
