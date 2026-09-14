from __future__ import annotations

import re


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()


def is_channel_promo_line(line: str) -> bool:
    normalized = normalize_text(line)
    if not normalized:
        return False
    if is_submission_line(line) or is_forward_channel_line(line):
        return True
    fixed_markers = ("在花频道", "茶馆水群", "投稿通道")
    if any(normalize_text(marker) in normalized for marker in fixed_markers):
        return True
    if len(line) > 80:
        return False
    if "投稿" not in normalized:
        return False
    community_terms = ("频道", "科技圈", "茶馆", "群组", "水群", "吹水", "网站")
    return sum(term in normalized for term in community_terms) >= 2


def is_submission_line(line: str) -> bool:
    return bool(re.match(r"^\s*投稿\s*[:：]\s*@?[A-Za-z0-9_]+(?:\s*)$", line))


def is_forward_channel_line(line: str) -> bool:
    return bool(re.match(r"^\s*Channel\s*[:：].*(?:t\.me/|4K影视屋)", line, flags=re.IGNORECASE))
