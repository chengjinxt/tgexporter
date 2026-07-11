from __future__ import annotations

import re
from pathlib import Path

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

REPLACEMENTS = {
    '"': "“",
    ":": "：",
    "/": "、",
    "\\": "、",
    "*": "",
    "?": "？",
    "<": "《",
    ">": "》",
    "|": "｜",
}


def sanitize_title(title: str, max_length: int = 90) -> str:
    value = title.strip()
    if looks_corrupt(value):
        value = "未命名文章"
    for old, new in REPLACEMENTS.items():
        value = value.replace(old, new)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "未命名文章"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"{value}_文章"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value


def looks_corrupt(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    question_count = stripped.count("?") + stripped.count("？")
    if question_count < 4:
        return False
    return question_count / max(len(stripped), 1) >= 0.25


def article_filename(date_key: str, daily_index: int, title: str) -> str:
    safe_title = sanitize_title(title)
    return f"{date_key}_{daily_index:03d}_{safe_title}.md"


def media_filename(
    date_key: str,
    daily_index: int,
    kind: str,
    media_index: int,
    title: str,
    extension: str,
) -> str:
    safe_title = sanitize_title(title)
    safe_kind = kind.upper()
    safe_extension = normalize_extension(extension)
    return f"{date_key}_{daily_index:03d}_{safe_kind}_{media_index:03d}_{safe_title}{safe_extension}"


def normalize_extension(extension: str | None) -> str:
    if not extension:
        return ".bin"
    value = extension.strip().lower()
    if not value:
        return ".bin"
    if not value.startswith("."):
        value = f".{value}"
    return re.sub(r"[^.a-z0-9]+", "", value) or ".bin"


def extension_from_file_path(file_path: str | None, fallback: str = ".bin") -> str:
    if not file_path:
        return fallback
    suffix = Path(file_path).suffix
    return normalize_extension(suffix or fallback)


def extension_from_mime(mime_type: str | None, fallback: str = ".bin") -> str:
    if not mime_type:
        return fallback
    known = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }
    return known.get(mime_type.lower(), fallback)
