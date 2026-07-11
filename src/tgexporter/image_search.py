from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from .link_enricher import build_opener

GOOGLE_IMAGE_RE = re.compile(r"https://encrypted-tbn\d\.gstatic\.com/images\?q=tbn:[^\"'\\\s<>]+")


def find_google_image_url(query: str, proxy_url: str | None = None, timeout: int = 12) -> str | None:
    search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"tbm": "isch", "q": query, "hl": "zh-CN"}
    )
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode("utf-8", errors="ignore")
    except OSError:
        return None
    for match in GOOGLE_IMAGE_RE.finditer(text):
        return html.unescape(match.group(0))
    return None

