from __future__ import annotations

import re


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()


def is_channel_promo_line(line: str) -> bool:
    normalized = normalize_text(line)
    if not normalized:
        return False
    fixed_markers = ("在花频道", "茶馆水群", "投稿通道")
    if any(normalize_text(marker) in normalized for marker in fixed_markers):
        return True
    if len(line) > 80:
        return False
    promo_terms = ("频道", "群组", "群", "投稿", "网站", "吹水")
    matched_terms = {term for term in promo_terms if term in line}
    return "频道" in matched_terms and "投稿" in matched_terms and len(matched_terms) >= 3
