from __future__ import annotations

import re

MARKDOWN_IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>[^\]]*)]\((?P<target>.*)\)\s*$")
MARKDOWN_LINK_LINE_RE = re.compile(r"^(?P<prefix>[^[]*)\[(?P<label>[^\]]*)]\((?P<target>.*)\)\s*$")


def markdown_image_line(line: str) -> tuple[str, str] | None:
    match = MARKDOWN_IMAGE_LINE_RE.match(line.strip())
    if not match:
        return None
    return match.group("alt"), normalize_markdown_target(match.group("target"))


def markdown_link_line(line: str) -> tuple[str, str, str] | None:
    match = MARKDOWN_LINK_LINE_RE.match(line.strip())
    if not match:
        return None
    return match.group("prefix"), match.group("label"), normalize_markdown_target(match.group("target"))


def normalize_markdown_target(target: str) -> str:
    value = target.strip()
    if not value:
        return ""
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")].strip()
    return value.strip('"').strip("'")


def is_external_markdown_target(target: str) -> bool:
    lower = target.lower()
    return lower.startswith(("http://", "https://", "data:", "file://", "#"))
